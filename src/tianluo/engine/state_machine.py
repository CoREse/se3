"""Core state machine implementation for the flow engine.

The StateMachine controls step transitions and execution flow.
"""

from __future__ import annotations

import copy
import json
import logging
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from .models import (
    FlowInstance,
    FlowStatus,
    State,
    Step,
    StepStatus,
    StepType,
    get_default_step_sequence,
    get_step_info,
)
from .plan_decomposition import (
    PLAN_DECOMPOSITION_KEY,
    PLAN_GRANULARITY_KEY,
    PlanModeError,
    PlanModeResolver,
    holistic_execution_mode,
)
from . import adjudication
from ..i18n import t
from .chat_history import _history_dir
from .llm_caller import clear_phase1_cache
from .review_scope import (
    ReviewBaseline,
    ReviewScopeManager,
    SelfCheckRoundController,
)
from .token_usage import accumulate_step_usage, UsageTotals
from ..usage import UsageRecord, UsageSummary, deduplicate_usage_records
from .issue_discovery import IssueDiscovery
from .issue_manager import IssueManager
from .persistence import PersistenceManager
from ..config import (
    ConfigError,
    insert_confirmation_steps,
    load_pricing_catalog,
    resolve_confirm_inputs,
    resolve_retired_always_on_confirm_inputs,
    WorkflowConfig,
)
from .. import __version__ as se3_version

logger = logging.getLogger(__name__)


class StateMachineError(Exception):
    """Base exception for state machine errors."""

    pass


class StepExecutionError(StateMachineError):
    """Error during step execution."""

    pass


class TransitionError(StateMachineError):
    """Error during state transition."""

    pass


class MergeCheckoutResolutionError(StateMachineError):
    """The main checkout for a worktree flow's merge-side steps is unresolvable.

    Raised when a worktree flow cannot positively resolve the main checkout the
    merge-side steps (``merge_integrate`` / ``version_reconcile``) must run in.
    Failing loudly is mandatory: silently falling back to the isolation worktree
    would run the merge / version reconcile in the wrong checkout, landing the
    branch and writing the version/changelog outside master.
    """

    pass


# Cap on automatic PARTIAL → re-run continuations for a holistic IMPLEMENT
# step (small task type, or a capability-mode plan that produced one group).
# Each continuation is a fresh paid LLM call and a partial result never
# reaches the FAILED decision path on its own, so without this bound a caller
# that keeps reporting "partial" would loop forever. Past the limit the step
# is persisted as FAILED and run.py routes it into its Retry/Skip/Abort
# decision path — further attempts then require an explicit user choice. The
# budget is sticky across resume so an automated resume loop cannot extend it.
_HOLISTIC_CONTINUATION_LIMIT = 3


def _reset_retry_counter_for_new_call(step: "Step") -> None:
    """Clear ``step.inputs['retry_count']`` at a transition point where the
    next LLM call is a fresh one (new discovery round, new fix iteration,
    new revision) rather than a retry of a previous attempt with the same
    intended prompt.

    Why this exists:
        ``inputs['retry_count']`` drives ``LLMCaller.external_attempt``,
        which flips ``is_retry=True`` and causes the caller to *replace*
        the step's carefully-built prompt with a retry-context wrapper
        ("previous attempt history + 'continue from where you left off'").
        That semantic is correct for same-call retries (e.g. user clicks
        Retry after FAILED, or the process was interrupted mid-call) but
        wrong for multi-round / iteration-based steps, whose next prompt
        is a new prompt with new inputs (``user_response``,
        ``fix_instructions``, ``revision_feedback``, ...). Without this
        reset, once any prior step-level retry bumps the counter, every
        subsequent round or iteration silently discards its own prompt and
        the LLM sees a stale snapshot of the previous call.

    Callers: ``_transition_to_fix``, ``_transition_to_revision``, and the
    discovery user-response branch in ``run.py``. See spec / tests for the
    exhaustive list.
    """
    step.inputs.pop("retry_count", None)


def _infer_fix_reason(trigger_step_type: str) -> str:
    reason_map = {
        "self_check": "self_check",
        "test": "test_failure",
        "e2e": "e2e_failure",
        "verify_spec": "spec_compliance",
    }
    # For unknown trigger types, return the trigger type itself rather than
    # silently mislabeling as "spec_compliance". Keeps future step types debuggable.
    return reason_map.get(trigger_step_type, trigger_step_type or "unknown")


def _latest_adjudicated_output(
    flow: "FlowInstance", key: str, exclude_step_id: Optional[str] = None
) -> Any:
    """Latest completed ADJUDICATE step's non-empty ``outputs[key]``.

    Walks ``step_history`` in reverse so multi-generational rulings resolve to
    the newest override. A later ADJUDICATE that left ``key`` empty (e.g. it
    only rewrote the plan, not the description) does NOT veto an older
    generation that did set it — the reverse walk simply skips empty values and
    keeps the still-live override in effect. Returns ``None`` when no ruling
    supplied ``key``.

    ``exclude_step_id`` skips one ADJUDICATE step, resolving to the effective
    text/plan *before* that ruling. The confirmation门 uses it so the reviewer
    compares a not-yet-approved ruling against the pre-ruling baseline instead
    of against the ruling's own unapproved rewrite (which, being at the tail, is
    otherwise what the newest-wins walk would surface).

    Modern routing consumes this helper only for the effective-description
    chain. Passing ``key="adjudicated_plan"`` remains supported for old flow
    display/recovery code, but a legacy plan override no longer changes modern
    IMPLEMENT or SELF_CHECK inputs.
    """
    if not (flow.state and flow.state.step_history):
        return None
    for sid in reversed(flow.state.step_history):
        if exclude_step_id is not None and sid == exclude_step_id:
            continue
        s = flow.state.steps.get(sid)
        if (
            s
            and s.step_type == StepType.ADJUDICATE
            and s.status in (StepStatus.COMPLETED, StepStatus.PARTIAL)
        ):
            val = s.outputs.get(key)
            if val:
                return val
    return None


def _latest_investigation_report(flow: "FlowInstance") -> Optional[Dict[str, Any]]:
    """Newest COMPLETED INVESTIGATE step's ``root_cause_report``, or ``None``.

    Reverse walk so the last round's verdict wins over earlier, superseded
    hypotheses. Used by the fix-loop path, which reuses the existing IMPLEMENT
    step object rather than building fresh inputs — without this the fix
    iterations would be the only implement calls that never see the root cause.
    """
    if not (flow.state and flow.state.step_history):
        return None
    for sid in reversed(flow.state.step_history):
        s = flow.state.steps.get(sid)
        if (
            s
            and s.step_type == StepType.INVESTIGATE
            and s.status == StepStatus.COMPLETED
        ):
            report = s.outputs.get("root_cause_report")
            if isinstance(report, dict) and report:
                return report
    return None


def _effective_task_description_base(
    flow: "FlowInstance", exclude_step_id: Optional[str] = None
) -> str:
    """Pre-interjection base of the effective task_description.

    Resolution order (highest priority first): a completed ADJUDICATE step's
    ``adjudicated_description`` (the covering patch that resolves a spec
    contradiction) > a completed DISCOVERY step's ``refined_description`` >
    the canonical ``flow.task_description``. Does NOT apply user_interjections
    — that's the ``_compose_effective_task_description`` step. Exposed
    separately so callers that need to RE-compose after appending an
    interjection (e.g. ``run.py:_handle_step_interrupt`` on a step whose
    inputs already carry a previously-composed task_description) can recover
    the un-decorated base without double-counting prior interjections that are
    already in the persisted list.

    ``exclude_step_id`` skips one ADJUDICATE ruling, yielding the base that was
    effective *before* it — used when building a confirmation门 that gates that
    same (still-unapproved) ruling, so the reviewer's baseline is the pre-ruling
    text, not the proposed rewrite.

    Why adjudicated must win over refined/original: the ruling is a *covering*
    patch, not an addendum. Making it the effective base is what pulls the dead
    clause out of the verbatim_quote source pool so a re-quote of the abolished
    text fails validation (see ``self_check._build_source_pool``); an
    additional-instructions approach would leave the contradiction in the pool.
    """
    adjudicated = _latest_adjudicated_output(
        flow, "adjudicated_description", exclude_step_id=exclude_step_id
    )
    if isinstance(adjudicated, str) and adjudicated:
        return adjudicated
    base = flow.task_description or ""
    # Walk step_history in reverse to pick up the latest completed
    # DISCOVERY step's refined_description.
    for sid in reversed(flow.state.step_history):
        s = flow.state.steps.get(sid)
        if (
            s
            and s.step_type == StepType.DISCOVERY
            and s.status in (StepStatus.COMPLETED, StepStatus.PARTIAL)
        ):
            refined = s.outputs.get("refined_description")
            if isinstance(refined, str) and refined:
                base = refined
            break
    return base


def _compose_effective_task_description(
    flow: "FlowInstance", exclude_step_id: Optional[str] = None
) -> str:
    """Compute the effective task_description for any step in the flow.

    Resolution order (matches ``_build_step_inputs``):
      1. ``flow.task_description`` is the canonical original.
      2. If a completed DISCOVERY step produced ``refined_description``,
         that overrides #1.
      3. If ``flow.state.context["user_interjections"]`` is non-empty,
         the composer appends the ``## Additional Instructions`` section.

    Used by both ``_build_step_inputs`` (creating fresh inputs) and
    ``_transition_to_fix`` (re-entering implement on fix loop). Without
    this shared helper, the fix loop would silently revert to the raw
    original task_description, dropping any discovery refinement and any
    Ctrl-C interjections — making mid-flow corrections invisible to every
    fix iteration.

    ``exclude_step_id`` is forwarded to ``_effective_task_description_base`` so a
    confirmation门 gating an unapproved ADJUDICATE ruling composes against the
    pre-ruling base (see that helper).
    """
    base = _effective_task_description_base(flow, exclude_step_id=exclude_step_id)
    interjections = flow.state.context.get("user_interjections", [])
    if interjections:
        from .task_description import compose_task_description_with_interjections
        base = compose_task_description_with_interjections(base, interjections)
    return base


# Cap on serialized previous_output size (bytes) to prevent unbounded
# accumulation across many fix iterations.
_PREVIOUS_OUTPUT_MAX_BYTES = 20_000

# Cap on issues stored per fix_history entry to prevent unbounded growth
# when a verify_spec round reports a large issue list.
_FIX_HISTORY_ISSUES_CAP = 10


def _declared_changed_paths(inputs: Mapping[str, Any]) -> List[str]:
    """Paths the implement step self-reported under ``changes_made``.

    Only used to admit git-ignored files into the review scope (the baseline
    enumerates with ``--exclude-standard`` and can never hold them); the
    reconstructed diff stays authoritative for everything git can see.
    """
    changes = inputs.get("changes_made") or {}
    if not isinstance(changes, dict):
        return []
    files_changed = changes.get("files_changed") or []
    if not isinstance(files_changed, list):
        return []
    out: List[str] = []
    for entry in files_changed:
        if isinstance(entry, str) and entry.strip():
            out.append(entry.strip())
        elif isinstance(entry, dict):
            path = entry.get("path") or entry.get("file_path")
            if isinstance(path, str) and path.strip():
                out.append(path.strip())
    return out


def _cap_issue_list(value: Any) -> list:
    """Return a capped list of issue dicts. Tolerates non-list input (e.g. bool,
    None, a single dict) so fix_history stays well-formed regardless of what a
    step placed under `spec_issues`/`issues`."""
    if not value:
        return []
    if isinstance(value, list):
        return value[:_FIX_HISTORY_ISSUES_CAP]
    return []


def _normalize_issue_fields(issues: list) -> list:
    """Normalize issue dicts so downstream consumers can use a single field name.

    self_check issues use ``severity`` while verify_spec issues use ``priority``.
    This function ensures every issue dict carries a ``severity`` key (canonical
    for fix_history) derived from whichever field is present, so that
    ``_format_fix_history`` in the implement step does not need a dual-access
    pattern.
    """
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        if "severity" not in issue and "priority" in issue:
            issue["severity"] = issue["priority"]
        elif "priority" not in issue and "severity" in issue:
            issue["priority"] = issue["severity"]
    return issues


class StateMachine:
    """Finite state machine for workflow execution.

    Controls the flow from one step to the next based on program logic,
    not LLM decisions. Each step is executed within the state machine,
    but the step's internal work (calling LLM, running tests, etc.) is
    handled by step handlers.
    """

    def __init__(
        self,
        project_root: Path,
        persistence: Optional[PersistenceManager] = None,
    ):
        """Initialize state machine.

        Args:
            project_root: Root directory of the project
            persistence: Optional persistence manager
        """
        self.project_root = Path(project_root)
        self.persistence = persistence or PersistenceManager(project_root)

        # Issue discovery support
        self._issue_manager: Optional[IssueManager] = None
        self._issue_discovery: Optional[IssueDiscovery] = None

        # Step handlers registry
        self._handlers: Dict[StepType, Callable[[Step, FlowInstance], Any]] = {}

        # Pre-implement test baseline capture: a background suite run launched at
        # flow start (concurrent with analyze/plan/confirm) whose handle is
        # awaited just before IMPLEMENT's first write. ``_baseline_key`` is the
        # cache key computed at launch time so ``_ensure_baseline_ready`` can
        # write the measured result back to the cache without recomputing.
        self._baseline_capture: Any = None
        self._baseline_key: Optional[str] = None

        # Transition rules: (from_step, condition) -> to_step
        self._transitions: Dict[tuple[StepType, Optional[str]], StepType] = {}

        self._setup_default_transitions()

    def _get_issue_discovery(self, flow: FlowInstance) -> Optional[IssueDiscovery]:
        """Get or create the IssueDiscovery instance for the current flow."""
        if self._issue_discovery is not None and self._issue_discovery.flow_id == flow.flow_id:
            return self._issue_discovery
        try:
            if self._issue_manager is None:
                self._issue_manager = IssueManager(self.project_root)
            self._issue_discovery = IssueDiscovery(self._issue_manager, flow.flow_id)
            return self._issue_discovery
        except Exception as e:
            logger.debug(f"Failed to initialize IssueDiscovery: {e}")
            return None

    def _setup_default_transitions(self) -> None:
        """Set up default transition rules."""
        # Linear flow through selected steps
        self._transitions = {}

    def register_handler(
        self,
        step_type: StepType,
        handler: Callable[[Step, FlowInstance], Any],
    ) -> None:
        """Register a handler for a step type.

        Args:
            step_type: Type of step to handle
            handler: Function that executes the step
        """
        self._handlers[step_type] = handler

    def create_flow(
        self,
        task_description: str,
        task_type: str = "feature",
        change_name: Optional[str] = None,
        is_worktree_mode: bool = False,
        plan_decomposition: Optional[str] = None,
        plan_granularity: Optional[str] = None,
    ) -> FlowInstance:
        """Create a new flow instance.

        Args:
            task_description: User's task description
            task_type: Type of task (feature, bugfix, review, etc.)
            change_name: Optional associated change name
            is_worktree_mode: Whether this flow runs in worktree isolation mode
                (``luo run --worktree``)
            plan_decomposition: Optional explicit capability/granular doctrine
                for PLAN. When absent, project configuration and then the
                capability default are used.
            plan_granularity: Optional explicit auto/single/conservative group
                pressure, meaningful only under the capability doctrine.

        Returns:
            New flow instance

        Raises:
            ConfigError: If workflow configuration is invalid (e.g.
                self_check_passes_required < 1).
        """
        # Fail-fast: validate workflow configuration before creating the flow.
        # Reset the cache first so we are not reusing a stale value from a
        # prior flow on the same StateMachine instance, then go through
        # ``_get_workflow_config`` so ``create_flow`` and the first
        # ``transition_to_next`` cannot disagree about what the yaml said.
        self._workflow_config_cache = None
        workflow_cfg = self._get_workflow_config()

        # Determine initial step sequence.
        # WHY no routing-driven trimming any more: the retired
        # implementation_strategy axis used to cut PLAN out of a "direct" flow,
        # which made this sequence a function of two independent decisions. It
        # is now a function of task type alone — PLAN is unconditional wherever
        # the default table has it, and the execution shape of the
        # PLAN -> IMPLEMENT segment is read downstream off the group count PLAN
        # actually emitted, except that a persisted ``plan_granularity:
        # single`` pins the one-call shape regardless of the count. Nothing
        # below may remove a step for plan-mode reasons; a one-group plan is a
        # legitimate, fully-planned flow.
        selected_steps = get_default_step_sequence(task_type)

        # Append optional steps from tianluo.yaml (e.g. summarize)
        selected_steps = self._apply_step_config(selected_steps)

        # Opt-in e2e: insert the E2E step after TEST when the project enabled it.
        # Ordered here — after the configured appends, before the merge pair and
        # the confirmation gates — because both of those anchor on positions in
        # the sequence they are handed (the merge pair on COMMIT, a CONFIRM on the
        # step it guards). Inserting later would place e2e on the wrong side of a
        # confirmation gate. MUST stay mirrored in analyze._update_flow_steps,
        # which re-derives the whole sequence from the default table.
        selected_steps = self._insert_e2e_step(selected_steps)

        # For a worktree flow, the release point is the merge — extend the
        # sequence with the two merge-side steps (integrate then reconcile). This
        # happens BEFORE confirmation insertion so a configured per-step gate
        # (e.g. ``version_reconcile: {reviewer: human}``) still wraps them.
        if is_worktree_mode:
            selected_steps = self._append_worktree_merge_steps(selected_steps)

        flow = FlowInstance(
            task_description=task_description,
            task_type=task_type,
            change_name=change_name,
            is_worktree_mode=is_worktree_mode,
            status=FlowStatus.INIT,
        )

        # Set up initial state
        flow.state.selected_steps = selected_steps
        flow.state.current_step_index = 0
        flow.state.context["task_description"] = task_description
        flow.state.context["task_type"] = task_type
        flow.state.context["project_root"] = str(self.project_root)
        try:
            PlanModeResolver.initialize_context(
                flow.state.context,
                explicit_decomposition=plan_decomposition,
                explicit_granularity=plan_granularity,
                configured_workflow=workflow_cfg,
            )
        except PlanModeError as exc:
            raise ConfigError(str(exc)) from exc

        selected_steps = self._insert_confirmation_steps(selected_steps)
        flow.state.selected_steps = selected_steps
        # Stash the main checkout the merge-side steps must run in, resolved once
        # at flow creation from the (possibly worktree) project_root. Read back by
        # ``_merge_step_cwd`` when a merge step is instantiated — kept stable here
        # so the value cannot drift with the transient project_root rebinding the
        # merge steps perform while executing.
        #
        # Only ever stash a POSITIVELY-resolved main checkout: a probe fault here
        # must NOT be papered over by stashing the (worktree) project_root, since
        # that stale value would later send the merge-side steps into the
        # isolation worktree. Flow creation itself stays tolerant of a transient
        # probe failure (git momentarily unavailable, worktree not fully wired
        # yet) — we simply leave the stash empty and let ``_merge_step_cwd``
        # strictly re-resolve at execution time, failing loudly there if the
        # main checkout still cannot be resolved (fail-before-executing, never
        # run-in-the-wrong-checkout).
        if is_worktree_mode:
            try:
                flow.state.context["merge_checkout_root"] = str(
                    self._resolve_main_checkout_root()
                )
            except MergeCheckoutResolutionError:
                logger.debug(
                    "Deferring merge_checkout_root resolution for worktree flow "
                    "at %s; main checkout unresolvable at creation, will re-resolve "
                    "strictly before any merge-side step runs.",
                    self.project_root,
                    exc_info=True,
                )

        # Create first step
        first_step_type = selected_steps[0] if selected_steps else StepType.ANALYZE
        first_step_inputs = {"task_description": task_description}

        # For discovery mode, mark the initial description
        if task_type == "discovery":
            first_step_inputs["initial_description"] = task_description
            first_step_inputs["discovery_mode"] = True

        first_step = Step(
            step_type=first_step_type,
            status=StepStatus.PENDING,
            inputs=first_step_inputs,
        )
        flow.state.add_step(first_step)
        flow.state.current_step_id = first_step.step_id

        # Save initial state
        self.persistence.save_flow(flow)

        logger.info(f"Created flow {flow.flow_id} for task: {task_description[:50]}...")

        return flow

    def _resolve_main_checkout_root(self) -> Path:
        """Resolve the main checkout for merge-side steps from ``self.project_root``.

        A worktree flow's ``self.project_root`` is the linked worktree; the merge
        must land on the main checkout. :func:`config.probe_main_repo_root` walks
        back from a linked worktree to its main repo, returns ``None`` only for
        the legitimate case that ``self.project_root`` is itself the main checkout
        (not a worktree), and raises on any genuine probe fault.

        This resolution is strict — used only for worktree merge-side steps, it
        must NOT degrade to ``self.project_root`` on failure. A silent fallback
        to the linked worktree would run the merge / version reconcile in the
        isolation worktree, landing the branch and writing the version/changelog
        outside master. A genuine probe fault is therefore re-raised as
        :class:`MergeCheckoutResolutionError` so the flow fails before any
        merge-side step executes in the wrong checkout.
        """
        from ..config import MainRepoProbeError, probe_main_repo_root

        resolved = Path(self.project_root).resolve()
        try:
            main = probe_main_repo_root(resolved)
        except MainRepoProbeError as exc:
            raise MergeCheckoutResolutionError(
                f"Cannot resolve the main checkout for worktree flow at "
                f"{self.project_root}; refusing to run merge-side steps in the "
                f"isolation worktree: {exc}"
            ) from exc
        # ``None`` means project_root is not a worktree — it IS the main checkout.
        return main if main is not None else resolved

    def _append_worktree_merge_steps(
        self, steps: list[StepType]
    ) -> list[StepType]:
        """Insert the two merge-side steps (integrate → reconcile) after ``commit``.

        The release point of a worktree flow is the merge, and it must be the
        immediate post-commit boundary — no ordinary/post-commit step (e.g. a
        configured ``summarize``) may run in the worktree between the branch
        commit and the merge. Delegates to the shared
        :func:`config.append_worktree_merge_steps` so this and
        ``analyze._update_flow_steps`` derive the same sequence.

        Idempotent: a step type already present (e.g. a resumed / re-derived
        sequence) is not duplicated.
        """
        from ..config import append_worktree_merge_steps

        return append_worktree_merge_steps(steps)

    def _merge_step_cwd(
        self, flow: FlowInstance, step_type: StepType
    ) -> Optional[str]:
        """Return the cwd override for a merge-side step, else ``None``.

        Merge steps run in the main checkout; every other step returns ``None``
        (uses the flow project_root). Prefers the value stashed at flow creation
        so it never drifts with the merge step's transient project_root rebind.
        """
        if step_type not in (StepType.MERGE_INTEGRATE, StepType.VERSION_RECONCILE):
            return None
        stashed = flow.state.context.get("merge_checkout_root") if flow.state else None
        if stashed:
            return str(stashed)
        return str(self._resolve_main_checkout_root())

    def _apply_step_config(self, steps: list[StepType]) -> list[StepType]:
        """Append optional steps from tianluo.yaml steps.append configuration.

        Args:
            steps: Original step sequence

        Returns:
            Modified step sequence with appended steps
        """
        from ..config import apply_step_config
        return apply_step_config(steps, self.project_root)

    def _insert_e2e_step(self, steps: list[StepType]) -> list[StepType]:
        """Insert the conditional ``E2E`` step (after ``TEST``) when e2e is enabled.

        Delegates to the shared :func:`config.insert_e2e_step` so this and
        ``analyze._update_flow_steps`` derive the same sequence — the analyze step
        rebuilds the sequence from the default table on every flow, so an
        insertion done only here would be silently dropped a moment later.
        No-op (and idempotent) when ``e2e.enabled`` is off.
        """
        from ..config import insert_e2e_step

        return insert_e2e_step(steps, self.project_root)

    def _insert_confirmation_steps(self, steps: list[StepType]) -> list[StepType]:
        """Insert CONFIRM steps after configured step types.

        Uses the shared insert_confirmation_steps function from config module
        to ensure consistency with analyze step handler.

        Args:
            steps: Original step sequence

        Returns:
            Modified step sequence with CONFIRM steps inserted
        """
        result = insert_confirmation_steps(steps, self.project_root)

        # Log inserted steps for debugging
        for i, step in enumerate(result):
            if step == StepType.CONFIRM:
                prev_step = result[i-1] if i > 0 else None
                if prev_step:
                    logger.debug(f"Inserted CONFIRM step after {prev_step.value}")

        return result

    def load_or_create_flow(
        self,
        task_description: Optional[str] = None,
        **kwargs,
    ) -> tuple[FlowInstance, bool]:
        """Load existing flow or create new one.

        Args:
            task_description: Task description for new flow
            **kwargs: Additional args for create_flow

        Returns:
            Tuple of (flow_instance, is_resumed)
        """
        existing = self.persistence.load_flow()

        if existing and existing.status not in (FlowStatus.COMPLETED,):
            # Found active or failed flow - offer to resume
            return existing, True

        # No active flow - create new
        if not task_description:
            raise StateMachineError("No active flow and no task description provided")

        return self.create_flow(task_description, **kwargs), False

    def _acquire_merge_step_lock(
        self, lock, flow: FlowInstance, step: Step, history_root: Path
    ) -> None:
        """Acquire the main-worktree merge lock, surfacing a wait state on contention.

        Mirrors run.py's ``_ensure_main_lock_for_step`` for the merge-side steps
        (merge_integrate / version_reconcile) of a worktree flow: a non-blocking
        probe first, and only if the lock is genuinely held by another run/merge
        does it mark the flow ``waiting_for_lock=True`` (persisted so the daemon /
        web UI show a queued-and-waiting flow rather than a generic running step),
        emit a streaming ``waiting_for_lock`` history anchor, then block until the
        holder releases. On acquisition the flag is cleared and, when a wait
        anchor was written, a matching ``record_lock_acquired`` clears the live
        transcript's "等待锁" row. All bookkeeping is best-effort — a persistence
        or history-write hiccup never blocks the actual lock acquisition.
        """
        from ..commands.merge.merge_lock import MergeLockBusy, MergeLockStale

        acquired = False
        try:
            lock.acquire(blocking=False)
            acquired = True
        except MergeLockStale:
            try:
                lock.acquire(blocking=False, break_stale=True)
                acquired = True
            except (MergeLockBusy, MergeLockStale):
                acquired = False
        except MergeLockBusy:
            acquired = False

        wrote_waiting = False
        if not acquired:
            flow.waiting_for_lock = True
            try:
                self.persistence.save_flow(flow)
            except Exception:  # noqa: BLE001 - bookkeeping must not block the lock
                logger.debug(
                    "failed to persist waiting_for_lock=True for %s",
                    flow.flow_id, exc_info=True,
                )
            try:
                from .chat_history import record_waiting_for_lock

                record_waiting_for_lock(
                    project_root=history_root,
                    flow_id=flow.flow_id,
                    step_id=step.step_id,
                    step_type=step.step_type.value,
                )
                wrote_waiting = True
            except Exception:  # noqa: BLE001
                logger.debug(
                    "failed to record waiting_for_lock for %s",
                    step.step_id, exc_info=True,
                )
            try:
                lock.acquire(blocking=True)
            except BaseException:
                # Any escape from the blocking acquire — a Ctrl+C queued behind
                # another merge (KeyboardInterrupt), or an OSError/stale-lock error
                # surfacing mid-wait — exits with waiting_for_lock=True already
                # persisted while no lock was actually acquired. Clear + persist the
                # flag before propagating (exception-symmetric, not just the
                # interrupt case), else engine.json records status=running +
                # waiting_for_lock=True for a dead process and the daemon/web console
                # keeps rendering a stale "等待主分支锁" badge until a later resume
                # re-enters the acquire path. Mirrors run.py's
                # _ensure_main_lock_for_step interrupt handling for the
                # synchronous-run lock wait.
                flow.waiting_for_lock = False
                try:
                    self.persistence.save_flow(flow)
                except Exception:  # noqa: BLE001 - best-effort on the failure path
                    logger.debug(
                        "failed to persist waiting_for_lock=False on lock-wait failure for %s",
                        flow.flow_id, exc_info=True,
                    )
                raise

        if flow.waiting_for_lock:
            flow.waiting_for_lock = False
            try:
                self.persistence.save_flow(flow)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "failed to persist waiting_for_lock=False for %s",
                    flow.flow_id, exc_info=True,
                )

        if wrote_waiting:
            try:
                from .chat_history import record_lock_acquired

                record_lock_acquired(
                    project_root=history_root,
                    flow_id=flow.flow_id,
                    step_id=step.step_id,
                    step_type=step.step_type.value,
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "failed to record lock-acquired for %s",
                    step.step_id, exc_info=True,
                )

    @contextmanager
    def _step_cwd_override(self, flow: FlowInstance, step: Step):
        """Run *step* against its ``cwd`` override, inside the merge lock.

        A step whose :attr:`Step.cwd` is set (the merge-side steps of a worktree
        flow) must execute in the MAIN checkout, not the isolated worktree the
        flow body ran in, and must be serialised against every other run/merge
        by the main-worktree mutex. This manager, for the duration of the step:

          * acquires the blocking main-worktree merge lock at the override root
            (queue-and-wait — the same mutex sync runs and ``luo merge`` take);
          * rebinds ``self.project_root`` and the flow's ``context['project_root']``
            to the override so the step's context / issue-discovery / spec paths
            all resolve to the main checkout consistently.

        ``self.persistence`` is deliberately NOT rebound: the flow's engine.json
        stays in its own home (the worktree) so ``--resume`` keeps finding it —
        only the *handler's* view of the project moves to the main checkout. On
        exit everything is restored and the lock released, even on error.

        A non-merge step with no override yields unchanged (the overwhelmingly
        common path), holding no lock and touching nothing. A merge-side step
        (MERGE_INTEGRATE / VERSION_RECONCILE) whose persisted header LOST its
        ``cwd`` (a corrupted / hand-edited engine.json, or a reconstructed resume
        flow) still mutates master — the handler's strict ``_resolve_merge_root``
        fallback resolves the main checkout and proceeds — so it must NOT run
        unserialised. Keying the lock on the presence of ``step.cwd`` would let
        such a step land branches / write versions on master concurrently with a
        ``luo merge``. So for a merge-side step we resolve the main checkout the
        same strict way and acquire the lock at that root regardless.
        """
        _merge_side = step.step_type in (
            StepType.MERGE_INTEGRATE,
            StepType.VERSION_RECONCILE,
        )
        if not step.cwd and not _merge_side:
            yield
            return

        # A merge-side step missing its cwd override still must hold the lock:
        # resolve the main checkout strictly (fail-loud on a genuine probe fault,
        # never degrading to the isolation worktree).
        override_root = Path(step.cwd) if step.cwd else self._resolve_main_checkout_root()
        saved_root = self.project_root
        # The merge lock file lives at ``<main checkout>/tianluo/state/merge.lock``;
        # the override root IS the main checkout, so it is the lock root.
        from ..commands.merge.merge_lock import MergeLock, is_lock_held_in_process

        # Same-process re-entry guard (mirrors the orchestrator's defence at
        # orchestrator._execute): if this process already holds the main-worktree
        # merge lock — e.g. a caller ran the whole worktree flow through run_flow
        # with acquire_main_lock=True, so _ensure_main_lock_for_step holds the
        # lock for the run's duration — a blocking flock against the same lock
        # file on a fresh fd would deadlock forever with no timeout. Detect the
        # already-held lock via the in-process registry and skip re-acquisition,
        # running the merge step under the externally-held lock.
        already_held = is_lock_held_in_process(override_root)
        lock = None if already_held else MergeLock(override_root, blocking=True)
        if lock is not None:
            # Surface the same running "等待锁" state a synchronous run shows while
            # it queues for the main-worktree mutex (run.py's
            # _ensure_main_lock_for_step): a second worktree flow reaching
            # merge_integrate concurrently must appear as running-and-waiting, not
            # a generic silent running step. The history anchor lands in the
            # flow's own home (``saved_root`` — self.project_root is not yet
            # rebound to the override here).
            self._acquire_merge_step_lock(lock, flow, step, saved_root)
        try:
            self.project_root = override_root
            if flow.state is not None:
                flow.state.context["project_root"] = str(override_root)
            yield
        finally:
            self.project_root = saved_root
            if flow.state is not None:
                # Restore to the flow's OWN home (``saved_root``), never to the
                # value read from persisted context. The rebind above is
                # transient and may have been persisted mid-step; if the process
                # died and this is a crash-resume re-run, the persisted context
                # already holds the override (main) root — restoring that would
                # leak the main checkout into every subsequent step permanently.
                # ``saved_root`` is self.project_root at entry (the worktree home,
                # reconstructed fresh each process from the actual checkout), so
                # it is the crash-independent correct value.
                flow.state.context["project_root"] = str(saved_root)
            if lock is not None:
                try:
                    lock.release()
                except Exception:  # noqa: BLE001 - release must never mask step result
                    logger.debug("merge lock release failed", exc_info=True)

    def run_step(
        self,
        flow: FlowInstance,
        step: Step,
        on_running: Optional[Callable[[Step], None]] = None,
    ) -> StepStatus:
        """Execute a single step.

        Args:
            flow: Current flow instance
            step: Step to execute
            on_running: Optional callback invoked exactly once, AFTER the step
                has actually transitioned to ``RUNNING`` and been persisted, and
                BEFORE its handler runs. The orchestrator uses this to emit
                ``STEP_STARTED`` so a "进行中" anchor is shown only for a step
                that genuinely entered RUNNING — never for a missing-handler
                step (which fails before this point) nor for a step whose
                pre-handler preprocessing (baseline / spec snapshot) raised.

        Returns:
            Final status of the step
        """
        # Freeze the pre-implement test baseline before IMPLEMENT's first write,
        # so the test step can tell inherited (baseline) failures from
        # introduced ones. Idempotent across fix-loop re-entries into implement.
        if step.step_type == StepType.IMPLEMENT:
            self._ensure_baseline_ready(flow)
            self._ensure_implementation_review_baseline(flow, step)

        # An interjection can mutate the effective requirements while a
        # SELF_CHECK step is pending or being retried. Refresh here, immediately
        # before persistence and the LLM call, so such a mutation invalidates an
        # incremental round even when no step-construction boundary occurred.
        if step.step_type == StepType.SELF_CHECK:
            self._refresh_self_check_scope(flow, step)

        # INVESTIGATE writes probes into the very tree the background baseline
        # suite is running against, so the two must never overlap. Settled here
        # — before the workspace snapshot below — so any files the suite leaves
        # behind are already part of the investigation's "before" picture.
        if step.step_type == StepType.INVESTIGATE:
            self._settle_baseline_before_investigation(flow)

        handler = self._handlers.get(step.step_type)

        if not handler:
            logger.warning(f"No handler registered for {step.step_type.value}")
            step.status = StepStatus.FAILED
            step.error_message = f"No handler for step type {step.step_type.value}"
            return step.status

        # Mark as running (clear any previous error from failed attempts)
        step.status = StepStatus.RUNNING
        step.error_message = None
        step.started_at = datetime.now()
        # This step is now genuinely (re-)producing its body via its handler, so
        # its in-memory inputs/outputs are the authoritative value — not a disk
        # proxy. Flip ``cold_loaded`` True (the execution/assignment path B3-i
        # requires) so: (1) a later keyed access via the lazy step dict no longer
        # re-fires the hydrator and wipes the freshly produced body back to {},
        # and (2) _split_flow detects the changed payload and rewrites this
        # step's cold file. Without this, a resumed step whose cold file was
        # missing/corrupt (cold_loaded stayed False after apply_cold(None)) would
        # have its re-produced outputs silently lost — the header would re-emit
        # the stale cold_ref on every save (issue #244 B3-i).
        step.cold_loaded = True

        # Freeze the investigation's net-zero-diff baseline here rather than
        # inside the handler, because the save_flow below is the last persistence
        # point before the (long) investigation call: a baseline first written by
        # the handler would live only in memory for that whole call, so a hard
        # kill mid-call (SIGKILL/OOM) would lose it and the `--resume`d round
        # would re-baseline onto its own unreverted experimental changes,
        # silently passing the guard. Placed after ``cold_loaded`` is flipped so
        # a later hydration cannot overwrite the freshly written inputs.
        # Idempotent, so a retry/resume keeps the original baseline.
        if step.step_type == StepType.INVESTIGATE:
            self._ensure_investigation_baseline(flow, step)

        flow.status = FlowStatus.RUNNING
        self.persistence.save_flow(flow)

        # The step is now genuinely RUNNING and persisted — notify the
        # orchestrator so it can emit STEP_STARTED here (not before the call,
        # where a missing handler or a failed pre-handler step would otherwise
        # leave a dangling "进行中" anchor that never reaches a terminal event).
        # Best-effort: a fault in the callback must never break the step.
        if on_running is not None:
            try:
                on_running(step)
            except Exception:
                logger.debug("on_running callback failed", exc_info=True)

        logger.info(f"Running step: {step.step_type.value}")

        # Step-scoped token-usage accumulator. Opened before the handler runs so
        # every LLM subprocess call made during this step (main call, retry,
        # rotation, two-phase JSON extraction) folds into one per-step total via
        # token_usage.add_call_usage. The yielded UsageTotals is captured here so
        # the finally block can read it even after the context manager has reset
        # the contextvar on exit (including the exception path).
        step_usage = None
        try:
            # Execute handler under the step usage scope. A step carrying a
            # ``cwd`` override (the merge-side steps) runs inside the main-worktree
            # merge lock with self.project_root rebound to the main checkout; an
            # ordinary step yields through _step_cwd_override unchanged.
            with self._step_cwd_override(flow, step), accumulate_step_usage() as step_usage:
                result = handler(step, flow)

            # Handler can return status or we infer from step object
            if isinstance(result, StepStatus):
                step.status = result
            else:
                # Assume success if no exception and step not marked failed
                if step.status == StepStatus.RUNNING:
                    step.status = StepStatus.COMPLETED

            # Only store result if handler didn't already set outputs
            if "result" not in step.outputs:
                step.outputs["result"] = result.value if isinstance(result, StepStatus) else result

        except Exception as e:
            logger.exception(f"Step {step.step_type.value} failed")
            step.status = StepStatus.FAILED
            step.error_message = str(e)
            step.error_details = getattr(e, "__traceback__", None)

        finally:
            step.completed_at = datetime.now()

            # Aggregate this step's token usage before persisting. Best-effort:
            # a fault here must never break the step / flow.
            #
            # Consumers read this as:
            #  * the CLI session summary — built from
            #    flow.state.session_usage_records, the authoritative per-call
            #    ledger, with session_token_usage as its legacy projection;
            #  * the web session badge — which renders the backend
            #    UsageSummary payload (never a client-side re-sum of emitted
            #    records; see applyUsageBadge in app.js).
            #
            # Both terminal and non-terminal runs publish `token_usage` so that
            # CLI renderers (`render_step_usage`) and WebUI report cards
            # (`buildStepUsageFootnote`) can display per-step usage regardless
            # of the step's current status. Previously only terminal runs
            # surfaced `token_usage`; non-terminal runs (PAUSED / REVISION_NEEDED)
            # wrote only `carried_token_usage`, so self_check / verify_spec /
            # confirm steps returning REVISION_NEEDED left no usage visible to
            # any consumer reading `outputs.token_usage`.
            #
            # A non-terminal run also carries the combined total forward in
            # `carried_token_usage` so the next run's `token_usage` includes
            # all prior non-emitting rounds, keeping the step-level display
            # complete across PAUSED/REVISION_NEEDED runs. The session records
            # still add only the current run's step_usage — not the combined
            # total — so there is no double-counting.
            #
            # `usage_records` / `usage_summary` publish the same per-step
            # records through the shared UsageSummary backend (billing-unit
            # de-duplicated actual cost + estimated/unknown classification),
            # and the per-step ledger accumulates across re-executions (FIX
            # iterations, retries, resumes), so step and session views can
            # never diverge by construction.
            try:
                # A run-unique discriminator keeps the legacy-record synthesis
                # (step_usage without embedded UsageRecords) from colliding
                # with the same step's earlier rounds in session-level dedup —
                # each round is genuinely new usage.
                run_discriminator = str(uuid.uuid4())[:8]
                current_records: List[UsageRecord] = []
                if step_usage is not None and (
                    step_usage.has_usage_records or not step_usage.is_empty()
                ):
                    current_records = step_usage.to_usage_records(
                        call_id=f"step:{step.step_id}:{run_discriminator}"
                    )
                    for record in current_records:
                        flow.state.add_session_usage_record(record)
                # Combine this run's usage with usage carried from prior
                # non-terminal (PAUSED / REVISION_NEEDED) runs of this step.
                carried = UsageTotals.from_dict(
                    step.outputs.get("carried_token_usage")
                )
                carried_records = (
                    carried.to_usage_records(
                        call_id=f"step:{step.step_id}:carried"
                    )
                    if carried.has_usage_records or not carried.is_empty()
                    else []
                )
                # Prior completed executions' records stay on the step ledger:
                # the per-step summary must cover every call/attempt the step
                # ran across initial implementation, FIX iterations, retries
                # and resumes — not just the most recent execution. The
                # dedup below collapses the carried overlap (carried embeds
                # these same records) and any replayed copies.
                prior_outputs = step.outputs.get("usage_records")
                prior_records = (
                    [
                        UsageRecord.from_dict(item)
                        for item in prior_outputs
                        if isinstance(item, dict)
                    ]
                    if isinstance(prior_outputs, list)
                    else []
                )
                combined_records = deduplicate_usage_records(
                    prior_records + carried_records + current_records
                )[0]
                combined = UsageTotals.from_usage_records(combined_records)
                # Publish `token_usage` for both terminal and non-terminal
                # runs so step-level renderers can display it.
                if combined.has_usage_records or not combined.is_empty():
                    step.outputs["token_usage"] = combined.to_dict()
                if combined_records:
                    step.outputs["usage_records"] = [
                        record.to_dict() for record in combined_records
                    ]
                    # Records are already persisted above, so the summary keeps
                    # the records-free shape — but it must still carry its
                    # aggregate token totals and completeness, otherwise a
                    # consumer reading step.outputs.usage_summary directly
                    # reconstructs a zero-token, unavailable summary.
                    #
                    # Model-provenance marking is skipped here: this compact
                    # summary travels without the per-call rows that an
                    # unknown-model count annotates, so a legacy-adapted
                    # record's missing model would only degrade the
                    # completeness label beside pure totals. The flow-level
                    # payloads (history CLI / daemon / WebUI) keep the marking
                    # and surface the unknown-model row there.
                    step.outputs["usage_summary"] = UsageSummary.summarize(
                        combined_records,
                        catalog=load_pricing_catalog(self.project_root),
                        mark_unknown_models=False,
                    ).to_dict_for_wire()
                if step.status in (
                    StepStatus.COMPLETED,
                    StepStatus.PARTIAL,
                    StepStatus.FAILED,
                ):
                    # Terminal: the step_completed/step_failed record will be
                    # emitted. Clear the carry — the published token_usage
                    # already reflects the full combined total.
                    step.outputs.pop("carried_token_usage", None)
                else:
                    # Non-terminal: also carry the combined total forward so
                    # the next run of this step can accumulate into it.
                    if combined.has_usage_records or not combined.is_empty():
                        step.outputs["carried_token_usage"] = combined.to_dict()
            except Exception:
                logger.debug("Failed to record step token usage", exc_info=True)
            self.persistence.save_flow(flow)

        logger.info(f"Step {step.step_type.value} finished with status: {step.status.value}")

        # B-class collection: collect discovered issues from whitelist steps
        if step.status in (StepStatus.COMPLETED, StepStatus.PARTIAL) and step.outputs.get("discovered_issues"):
            try:
                discovery = self._get_issue_discovery(flow)
                if discovery:
                    discovery.collect_issues_from_output(
                        flow,
                        step.step_type.value,
                        step.outputs,
                    )
            except Exception as e:
                logger.warning(f"Failed to collect discovered issues: {e}")

        return step.status

    @staticmethod
    def _is_holistic_implement_step(flow: FlowInstance, step: Step) -> bool:
        """Identify whole-task IMPLEMENT paths (small type, or a single group).

        Delegates to ``plan_decomposition.holistic_execution_mode`` — the same
        predicate the IMPLEMENT handler uses to pick its executor — so the
        auto-continuation gate here can never disagree with the shape that
        actually ran.
        """
        return holistic_execution_mode(
            task_type=step.inputs.get("task_type") or flow.task_type,
            inputs=step.inputs,
            context=flow.state.context,
        ) is not None

    def transition_to_next(
        self, flow: FlowInstance,
    ) -> Optional[Step]:
        """Transition to the next step based on current state.

        Handles normal progression and review loop (going back to previous step).

        Args:
            flow: Current flow instance

        Returns:
            Next step if transition successful, None if flow complete
        """
        # Invalidate workflow config cache so each transition sees fresh config,
        # but within a single transition _get_workflow_config is memoized.
        self._workflow_config_cache = None
        # Same per-transition memoization for the self_check chain resolution
        # (needed to derive the effective pass count from a nested chain).
        self._self_check_resolution_cache = None
        # ...and for the investigation round cap, which the INVESTIGATE loop
        # branch below reads (possibly twice, via _build_step_inputs).
        self._investigation_config_cache = None

        # Resume-with-invalid-yaml safety net: when this StateMachine instance
        # has no ``_workflow_config_last_good`` (e.g. first transition after
        # a fresh process resume) and the yaml is invalid, ``_get_workflow_config``
        # re-raises ``ConfigError``. Without this guard the exception would
        # propagate while the on-disk flow remains in ``RUNNING`` status,
        # leaving the user with a stuck flow they cannot easily recover. Mark
        # the flow ``FAILED`` and persist BEFORE re-raising so the on-disk
        # state matches reality. Subsequent transitions that hit a hot-edit
        # mid-flow are unaffected — those have ``last_good`` cached and never
        # raise here.
        try:
            max_fix_iterations = self._get_max_fix_iterations()
        except ConfigError as e:
            logger.error(
                "Workflow config invalid on resume (no prior good config "
                "cached); marking flow FAILED before re-raising: %s", e,
            )
            flow.status = FlowStatus.FAILED
            flow.completed_at = datetime.now()
            try:
                self.persistence.save_flow(flow)
            except Exception:
                logger.exception(
                    "Failed to persist FAILED status while handling ConfigError; "
                    "the flow may remain RUNNING on disk"
                )
            raise

        current_step = flow.state.get_current_step()

        if not current_step:
            raise TransitionError("No current step")

        # Check if current step completed successfully
        if current_step.status not in (StepStatus.COMPLETED, StepStatus.PARTIAL, StepStatus.PAUSED, StepStatus.REVISION_NEEDED):
            logger.warning(
                f"Cannot transition from {current_step.status.value} step"
            )
            return None

        # A whole-task IMPLEMENT is complete only when its structured result
        # says complete and carries no unfinished work. Keep the same Step and
        # workspace live for another autonomous call; multi-group execution
        # retains its historical PARTIAL-forwarding semantics. The gate must
        # not fire when the transition itself IS the user's Skip decision —
        # re-capturing there would either resurrect the identical failure
        # prompt (budget exhausted) or convert the Skip into an unrequested
        # paid re-run (budget not exhausted). run.py marks the step with a
        # one-shot ``holistic_skip_forced`` input flag for that choice; every
        # other path (normal completion, crash-resume) keeps the automatic
        # loop. The flag lives on the step rather than a call parameter so
        # the intent is persisted flow state and the public transition
        # signature stays ``transition_to_next(flow)`` for all callers.
        skip_forced = bool(current_step.inputs.pop("holistic_skip_forced", False))
        if (
            not skip_forced
            and current_step.step_type == StepType.IMPLEMENT
            and self._is_holistic_implement_step(flow, current_step)
            and (
                current_step.status == StepStatus.PARTIAL
                or bool(current_step.outputs.get("incomplete_tasks"))
                or str(
                    current_step.outputs.get("completion_status", "complete")
                ).strip().lower() != "complete"
            )
        ):
            previous_output = {
                key: copy.deepcopy(current_step.outputs.get(key))
                for key in (
                    "files_changed",
                    "tests_added",
                    "test_mapping",
                    "summary",
                    "completion_status",
                    "incomplete_tasks",
                )
                if key in current_step.outputs
            }
            current_step.inputs["previous_output"] = previous_output
            current_step.inputs["resumed"] = True
            current_step.inputs["retry_count"] = (
                current_step.inputs.get("retry_count", 0) + 1
            )
            continuations = (
                current_step.inputs.get("holistic_continuations", 0) + 1
            )
            current_step.inputs["holistic_continuations"] = continuations
            # Bound the automatic continuation loop: past the limit, persist
            # FAILED so run.py routes into its Retry/Skip/Abort decision path
            # instead of silently paying for another agent call. run_step has
            # no FAILED-status guard, so returning the step here would just
            # re-invoke the handler — the persisted FAILED is what stops it.
            if continuations > _HOLISTIC_CONTINUATION_LIMIT:
                current_step.status = StepStatus.FAILED
                current_step.error_message = t(
                    "engine.implement.holistic_partial_exhausted",
                    limit=_HOLISTIC_CONTINUATION_LIMIT,
                )
                current_step.error_details = None
                self.persistence.save_flow(flow)
                return None
            current_step.status = StepStatus.PENDING
            current_step.error_message = None
            current_step.error_details = None
            # A partial summary is a completed Phase 1 result, not a schema-
            # extraction retry. The continuation must call the implementation
            # agent again while history supplies its retry context.
            clear_phase1_cache(
                self.project_root, flow.flow_id, current_step.step_id,
            )
            self.persistence.save_flow(flow)
            return current_step

        # Handle the fix loop: TEST, E2E, SELF_CHECK, or INVARIANT_CHECK returning
        # REVISION_NEEDED (the anchored INVARIANT_CHECK replaces the retired
        # SPEC_GATE/verify_spec as the diff-vs-recorded-invariant gate). The
        # deprecated VERIFY_SPEC is retained in the set so a pre-refactor
        # persisted flow can still resume its fix loop. All share the same
        # global max_fix_iterations exhaustion bound and route back to the
        # implement fix loop.
        #
        # E2E joins this set rather than growing its own routing: a failed
        # scenario is a code defect exactly like a failed unit test, so it must
        # share the same iteration budget and the same exhaustion outcome (an
        # issue via create_from_fix_loop_exhaustion). Its *environment* failures
        # never arrive here — the handler maps those to FAILED with remediation
        # guidance, because no code change makes a missing container runtime
        # appear.
        if (
            current_step.step_type in (
                StepType.TEST,
                StepType.E2E,
                StepType.SELF_CHECK,
                StepType.INVARIANT_CHECK,
                StepType.VERIFY_SPEC,
            )
            and current_step.status == StepStatus.REVISION_NEEDED
        ):
            # max_fix_iterations <= 0 is the sentinel for "unlimited" — skip
            # the exhaustion check entirely. Config rejects negatives at
            # load time, so in practice the sentinel is exactly 0.
            current_iteration = flow.state.get_fix_iteration()
            is_unlimited = max_fix_iterations <= 0

            if not is_unlimited and current_iteration >= max_fix_iterations:
                logger.error(
                    f"Max fix iterations ({max_fix_iterations}) reached — stopping flow as FAILED"
                )
                print(
                    "\n"
                    + t(
                        "engine.fixloop.exhausted",
                        max_iterations=max_fix_iterations,
                    )
                    + "\n"
                )
                # A-class trigger: create issue for fix loop exhaustion
                try:
                    discovery = self._get_issue_discovery(flow)
                    if discovery:
                        discovery.create_from_fix_loop_exhaustion(flow, current_step)
                except Exception as e:
                    logger.warning(f"Failed to create fix-loop exhaustion issue: {e}")
                flow.status = FlowStatus.FAILED
                return None
            else:
                # Adjudication routing (fix-loop 警察). Only SELF_CHECK feeds the
                # cross-round ledger, so only a SELF_CHECK-sourced REVISION_NEEDED
                # can trip the oscillation triggers; TEST / INVARIANT_CHECK keep
                # the original fix routing unchanged. Evaluated BEFORE
                # _transition_to_fix, after the max_fix_iterations exhaustion
                # guard above (so the global bound still caps a flow that keeps
                # adjudicating without converging).
                if current_step.step_type == StepType.SELF_CHECK:
                    self._self_check_round_controller(flow).mark_findings()
                    adjudicate_step = self._maybe_transition_to_adjudicate(
                        flow, current_step, current_iteration,
                    )
                    if adjudicate_step:
                        return adjudicate_step
                fix_step = self._transition_to_fix(flow, current_step)
                if fix_step:
                    return fix_step
                # No implement step found — fall through to normal progression
                logger.info("Fix transition returned None, falling through to next step")

        # Handle the ADJUDICATE ruling reflow (fix-loop 警察). A ruling changes
        # the *spec*, not the code, so re-running IMPLEMENT/TEST would chase a
        # knot the ruling already dissolved. Adjudication is unconfirmed by
        # default: unless `adjudicate` is explicitly opted into
        # confirmation.steps, `_maybe_confirm_adjudication` returns None and the
        # ruling auto-passes; only when opted in is a description-changing ruling
        # gated behind the confirmation门 (human review via tianluo/calls, or an LLM
        # reviewer) first. Once confirmation clears (or when none is required),
        # the pending fix_instructions are dropped (superseded, recorded in the
        # ADJUDICATE outputs for audit) and the flow re-runs SELF_CHECK directly
        # at pass #1.
        if (
            current_step.step_type == StepType.ADJUDICATE
            and current_step.status in (StepStatus.COMPLETED, StepStatus.PARTIAL)
        ):
            # No-op ruling (review_divergence — no real contradiction): route the
            # flow straight into IMPLEMENT with the triggering round's untouched
            # fix_instructions, as if ADJUDICATE had never been inserted. Routing
            # consumes the handler's single authoritative ``adjudication_noop``
            # flag rather than re-deriving contradiction_type here — the handler
            # owns the full benign-vs-contradiction verdict (incl. the
            # review_divergence-with-patch discard), so mirroring that judgement in
            # the router would only invite drift.
            if current_step.outputs.get("adjudication_noop"):
                return self._transition_after_adjudicate_noop(flow, current_step)
            confirm_step = self._maybe_confirm_adjudication(flow, current_step)
            if confirm_step:
                return confirm_step
            reflow_step = self._transition_after_adjudicate(flow, current_step)
            if reflow_step:
                return reflow_step
            # No SELF_CHECK slot to re-run — fall through to normal progression.
            logger.info(
                "Adjudication reflow found no SELF_CHECK slot; falling through"
            )

        # Handle review loop: if current step is CONFIRM and revision was requested
        if current_step.step_type == StepType.CONFIRM:
            review_result = current_step.outputs.get("review_result", {})
            approved = review_result.get("approved", True)

            if approved:
                # Approval received - continue to next step
                logger.info(f"Confirmation approved for {review_result.get('step_to_review_type', 'unknown')}")
                # A CONFIRM approving an ADJUDICATE ruling reflows exactly like a
                # direct ADJUDICATE completion: skip IMPLEMENT/TEST, re-run
                # SELF_CHECK at pass #1, and count the re-run as a fix iteration
                # (the increment lives in _transition_after_adjudicate so it fires
                # once on the landing, never on a rejected re-ruling cycle).
                # No branch for the no-op path here: a no-op ruling carries no
                # patch, so ``_maybe_confirm_adjudication`` never inserts a CONFIRM
                # for it — an approved-CONFIRM reflex can only ever land a
                # real-contradiction ruling, which always reflows via
                # ``_transition_after_adjudicate``.
                if review_result.get("step_to_review_type") == StepType.ADJUDICATE.value:
                    adj_id = review_result.get("step_to_review_id")
                    adj_step = flow.state.steps.get(adj_id) if adj_id else None
                    if adj_step:
                        reflow_step = self._transition_after_adjudicate(flow, adj_step)
                        if reflow_step:
                            return reflow_step
            else:
                # Revision requested - go back to the step being reviewed
                # Get step_to_review_id from review_result (set by confirm_handler)
                step_to_review_id = review_result.get("step_to_review_id")
                revision_step = self._transition_to_revision(flow, current_step, step_to_review_id)
                if revision_step:
                    return revision_step
                # If transition failed, continue to normal flow (will likely fail later)

        requirement_reflow = self._maybe_reflow_self_check_for_requirements(
            flow, current_step
        )
        if requirement_reflow is not None:
            return requirement_reflow

        # Handle N-pass self_check and the incremental -> full-closure boundary.
        if (
            current_step.step_type == StepType.SELF_CHECK
            and current_step.status == StepStatus.COMPLETED
        ):
            # Persist the defer-fix stash (item 1) the pass wrote back so the
            # next pass (created below) inherits the accumulated issue set. A
            # clean pass that left the stash untouched still echoes it, so this
            # is a uniform mirror; ``_build_step_inputs`` resets it at pass #1.
            if "self_check_deferred_issues" in current_step.outputs:
                flow.state.context["self_check_deferred_issues"] = copy.deepcopy(
                    current_step.outputs["self_check_deferred_issues"]
                )
            if current_step.inputs.get("self_check_round_id"):
                controller = self._self_check_round_controller(flow)
                effective_description = _compose_effective_task_description(flow)
                if controller.requirements_changed(effective_description):
                    controller.force_full("effective_requirements_changed")
                    logger.info(
                        "Effective requirements changed during SELF_CHECK; "
                        "discarding the active scope and restarting at full pass #1"
                    )
                    # The mutation ends the pass chain, but a deferred stash
                    # accumulated by earlier passes must still reach the fix
                    # loop. ``_build_step_inputs`` resets the stash at pass #1,
                    # so capture it here and re-inject it into the new full
                    # round's inputs — the new round funnels it into the fix
                    # loop exactly as a mid-chain pass would have.
                    deferred_before_reset = copy.deepcopy(
                        flow.state.context.get("self_check_deferred_issues") or []
                    )
                    repeat_step = self._create_self_check_repeat_step(
                        flow, advance_pass=False,
                    )
                    if deferred_before_reset:
                        flow.state.context["self_check_deferred_issues"] = (
                            deferred_before_reset
                        )
                        repeat_step.inputs["self_check_deferred_issues"] = (
                            copy.deepcopy(deferred_before_reset)
                        )
                        self.persistence.save_flow(flow)
                    return repeat_step

                passes_required = int(
                    current_step.inputs.get("self_check_passes_required", 1) or 1
                )
                pass_index = int(
                    current_step.inputs.get("self_check_pass_index", 1) or 1
                )
                if pass_index < passes_required:
                    logger.info(
                        "Self-check pass %d/%d completed; creating repeat pass #%d/%d",
                        pass_index, passes_required, pass_index + 1, passes_required,
                    )
                    return self._create_self_check_repeat_step(flow)

                unflushed = self._unflushed_deferred_issues(flow, current_step)
                round_key = self._deferred_flush_round_key(current_step)
                attempted = flow.state.context.get(
                    "self_check_deferred_flush_attempted"
                )
                already_attempted = (
                    isinstance(attempted, str)
                    and attempted
                    and attempted == round_key
                )
                stash = flow.state.context.get("self_check_deferred_issues") or []
                stash = list(stash) if isinstance(stash, list) else []
                rescue_skipped = (
                    already_attempted
                    and bool(stash)
                    and not current_step.outputs.get("fix_needed")
                    and "self_check_deferred_issues" not in (
                        current_step.outputs or {}
                    )
                )
                if rescue_skipped:
                    # The rescue pass for this round already ran (or was
                    # itself skipped) and the stash is still unconsumed.
                    # Routing another rescue pass would loop a
                    # repeatedly-skipped pass; the validated findings must
                    # instead go to the fix loop NOW — the one destination
                    # the check-step contract permits.
                    logger.warning(
                        "SELF_CHECK deferred-issue rescue for round %s was "
                        "attempted but left %d validated finding(s) "
                        "unflushed; routing them into the fix loop",
                        round_key, len(stash),
                    )
                    self._route_deferred_into_fix_loop(
                        flow, current_step, stash
                    )
                    # Mirror the normal finding-bearing path: close the
                    # round with findings and honor the same adjudication
                    # routing before entering the fix loop.
                    self._self_check_round_controller(flow).mark_findings()
                    adjudicate_step = self._maybe_transition_to_adjudicate(
                        flow, current_step, flow.state.get_fix_iteration(),
                    )
                    if adjudicate_step:
                        return adjudicate_step
                    fix_step = self._transition_to_fix(flow, current_step)
                    if fix_step:
                        return fix_step
                if unflushed:
                    # The pass chain is ending with validated findings still in
                    # the stash — the final pass never ran its own flush (a
                    # FAILED pass force-completed via the Skip gate produces no
                    # outputs). Closing the round here would silently discard
                    # them, the one outcome the check-step contract forbids, so
                    # re-run the terminal pass with the stash re-injected; its
                    # chain-tail flush funnels them into the fix loop.
                    logger.warning(
                        "SELF_CHECK pass chain ended with %d deferred issue(s) "
                        "never flushed; re-running the terminal pass to route "
                        "them into the fix loop",
                        len(unflushed),
                    )
                    flow.state.context["self_check_deferred_flush_attempted"] = (
                        round_key
                    )
                    repeat_step = self._create_self_check_repeat_step(
                        flow, advance_pass=False,
                    )
                    flow.state.context["self_check_deferred_issues"] = copy.deepcopy(
                        unflushed
                    )
                    repeat_step.inputs["self_check_deferred_issues"] = copy.deepcopy(
                        unflushed
                    )
                    self.persistence.save_flow(flow)
                    return repeat_step

                if controller.complete_clean():
                    logger.info(
                        "Incremental SELF_CHECK round completed cleanly; "
                        "scheduling the required full closure round"
                    )
                    return self._create_self_check_repeat_step(
                        flow, advance_pass=False,
                    )
                # A clean full round (initial, forced, or closure) is the sole
                # route to the next selected quality gate.
            else:
                # Legacy persisted rounds have no scope metadata. Preserve their
                # historical N-pass behavior instead of rewriting a path that
                # was already underway before diff-scoped review existed.
                passes_required = self._get_self_check_passes_required()
                consecutive_passes = self._count_consecutive_self_check_completed(flow)
                if consecutive_passes < passes_required:
                    next_pass_index = consecutive_passes + 1
                    logger.info(
                        f"Self-check pass {consecutive_passes}/{passes_required} completed; "
                        f"creating repeat pass #{next_pass_index}/{passes_required}"
                    )
                    repeat_step = self._create_self_check_repeat_step(
                        flow, advance_pass=False,
                    )
                    return repeat_step

        # Handle the bounded investigation loop: an INVESTIGATE round that did
        # not reach a conclusive root cause schedules another round, up to
        # ``investigation.max_iterations`` (0 = unlimited).
        #
        # INVARIANT: this loop NEVER uses REVISION_NEEDED. The REVISION_NEEDED
        # branch above is hardcoded to (TEST, E2E, SELF_CHECK, INVARIANT_CHECK,
        # VERIFY_SPEC); an INVESTIGATE returning REVISION_NEEDED would skip it,
        # fall through to the plain "advance to the next selected step" logic
        # below, and the loop would silently never happen — no error, no round 2.
        # The handler therefore always returns COMPLETED and the router decides
        # here, exactly as the self_check N-pass does one branch up.
        #
        # The ``conclusive`` KEY (not its value) is what gates the branch: only
        # ``investigate_handler`` writes it, and only on a round that actually
        # produced a report. run.py's failure gate implements "Skip" by
        # force-setting the FAILED step to COMPLETED and calling this method, so
        # a skipped round arrives here indistinguishable from a real one except
        # for the missing key. Keying off ``outputs.get("conclusive")`` alone
        # would read the absent verdict as "not conclusive" and schedule another
        # round — Skip would loop instead of skipping, and each new round is a
        # fresh Step whose baseline is re-taken on the still-unreverted tree,
        # reopening exactly the re-baseline hole the persisted baseline closes.
        if (
            current_step.step_type == StepType.INVESTIGATE
            and current_step.status == StepStatus.COMPLETED
            and "conclusive" in current_step.outputs
        ):
            conclusive = bool(current_step.outputs.get("conclusive"))
            max_rounds = self._get_investigation_max_iterations()
            rounds_done = self._count_consecutive_investigate_completed(flow)
            is_unlimited = max_rounds <= 0

            if not conclusive and (is_unlimited or rounds_done < max_rounds):
                logger.info(
                    "Investigation round %d/%s was not conclusive; scheduling "
                    "round %d",
                    rounds_done,
                    "unlimited" if is_unlimited else str(max_rounds),
                    rounds_done + 1,
                )
                return self._create_investigate_repeat_step(flow)

            if not conclusive:
                # Budget exhausted without a conclusive cause. This is NOT a
                # flow failure: the best current hypothesis is still worth
                # planning against — it just has to be labelled low-confidence
                # so PLAN/IMPLEMENT treat it as a lead, not a finding.
                flow.state.context["investigation_exhausted"] = True
                logger.warning(
                    "Investigation exhausted %d round(s) without a conclusive "
                    "root cause; continuing with the best current hypothesis "
                    "(marked low-confidence downstream)",
                    rounds_done,
                )
            # Conclusive, or exhausted — fall through to normal progression.

        # Find next step in selected sequence
        selected = flow.state.selected_steps
        current_index = flow.state.current_step_index
        if current_index >= len(selected) or selected[current_index] != current_step.step_type:
            try:
                current_index = selected.index(current_step.step_type)
            except ValueError:
                raise TransitionError(f"Current step {current_step.step_type} not in selected sequence")

        if current_index >= len(selected) - 1:
            # Flow complete
            logger.info("Flow completed - all steps finished")
            flow.status = FlowStatus.COMPLETED
            flow.completed_at = datetime.now()
            # Advance the step index to the total step count so the unified
            # "completed steps / total steps" semantics report total/total
            # (e.g. 13/13) and progress 1.0 to every consumer of engine state
            # (the daemon aggregator, history, the web console). This is safe
            # for resume: ``transition_to_next`` self-heals an out-of-range
            # index via ``selected.index(current_step.step_type)`` above, and
            # ``_current_step`` keys off ``current_step_id``, not this index.
            flow.state.current_step_index = len(selected)
            self.persistence.save_flow(flow)
            return None

        # Create next step
        next_step_type = selected[current_index + 1]
        next_step = Step(
            step_type=next_step_type,
            status=StepStatus.PENDING,
            inputs=self._build_step_inputs(flow, next_step_type),
            cwd=self._merge_step_cwd(flow, next_step_type),
        )

        flow.state.add_step(next_step)
        flow.state.current_step_id = next_step.step_id
        flow.state.current_step_index = current_index + 1

        self.persistence.save_flow(flow)

        logger.info(f"Transitioned to step: {next_step_type.value}")

        return next_step

    def _transition_to_revision(
        self,
        flow: FlowInstance,
        confirm_step: Step,
        step_to_review_id: Optional[str],
    ) -> Optional[Step]:
        """Transition back to the step being reviewed for revision.

        Args:
            flow: Current flow instance
            confirm_step: The confirm step that triggered the revision
            step_to_review_id: ID of the step to re-run

        Returns:
            The step being revised, or None if failed
        """
        if not step_to_review_id:
            logger.warning("No step_to_review_id provided for revision")
            return None

        step_to_review = flow.state.steps.get(step_to_review_id)
        if not step_to_review:
            logger.warning(f"Step {step_to_review_id} not found for revision")
            return None

        # Get feedback
        feedback = confirm_step.outputs.get("revision_feedback", "")
        iteration = flow.state.increment_review_iteration(step_to_review_id)

        logger.info(f"Transitioning to revision of {step_to_review.step_type.value} (iteration {iteration})")

        # Clear any Phase 1 cache — revision means a full fresh LLM call
        clear_phase1_cache(self.project_root, flow.flow_id, step_to_review_id)

        # Reset the step for re-execution
        step_to_review.status = StepStatus.PENDING
        step_to_review.inputs["revision_feedback"] = feedback
        step_to_review.inputs["is_revision"] = True
        step_to_review.inputs["revision_iteration"] = iteration
        # Pass previous output so the LLM knows what to revise
        previous_output = {
            k: v for k, v in step_to_review.outputs.items()
            if not k.startswith("_")
        }
        step_to_review.inputs["previous_output"] = previous_output
        step_to_review.error_message = None
        step_to_review.error_details = None
        # A revision is a NEW LLM call with a revision prompt (containing
        # revision_feedback + previous_output), not a retry of the prior
        # call. Clear any stale retry counter so the revision prompt isn't
        # discarded by the LLMCaller retry-context path.
        _reset_retry_counter_for_new_call(step_to_review)
        # Keep the outputs for reference, but mark that they may be outdated
        step_to_review.outputs["_is_outdated"] = True

        # A rejected CONFIRM re-enters IMPLEMENT with revision feedback — a
        # real code change. Capture a fresh fix baseline so the next
        # SELF_CHECK round's incremental diff is relative to the state
        # immediately before THIS change, not to a stale baseline from an
        # earlier fix iteration.
        if step_to_review.step_type == StepType.IMPLEMENT:
            self._capture_fix_review_baseline(flow, iteration)

        # A confirmation revision of SELF_CHECK is NOT a fix: no code changed
        # since the rejected round, so scoping the re-run to the previous fix
        # delta would leave the reviewer's feedback ungroundable whenever it
        # names a defect outside that delta. Incremental scope is reserved for
        # the round that follows an actual FIX.
        if step_to_review.step_type == StepType.SELF_CHECK:
            self._self_check_round_controller(flow).force_full(
                "confirmation_revision"
            )

        # Update flow state to point back to this step
        flow.state.current_step_id = step_to_review_id

        # Find the index of this step type in selected_steps
        try:
            step_index = flow.state.selected_steps.index(step_to_review.step_type)
            flow.state.current_step_index = step_index
        except ValueError:
            logger.warning(f"Step type {step_to_review.step_type} not in selected sequence")

        self.persistence.save_flow(flow)

        # The feedback body is reviewer-authored payload: passed through as data,
        # only the surrounding chrome is translated.
        shown_feedback = f"{feedback[:200]}..." if len(feedback) > 200 else feedback

        print(f"\n{'='*60}")
        print(t("engine.revision.banner_title", step=step_to_review.step_type.value.upper()))
        print(f"{'='*60}")
        print(t("engine.revision.iteration", iteration=iteration))
        print(t("engine.revision.feedback", feedback=shown_feedback))
        print(f"{'='*60}\n")

        return step_to_review

    def _transition_to_fix(
        self,
        flow: FlowInstance,
        trigger_step: Step,
    ) -> Optional[Step]:
        """Transition from TEST, SELF_CHECK, or INVARIANT_CHECK back to IMPLEMENT for fixing issues.

        This implements the test/self-check/invariant-fix loop. When issues are
        detected (by TEST, SELF_CHECK, INVARIANT_CHECK, or the deprecated
        VERIFY_SPEC step), the step returns REVISION_NEEDED and this method
        transitions back to the implement step with fix context.

        Args:
            flow: Current flow instance
            trigger_step: The step (TEST, SELF_CHECK, INVARIANT_CHECK, ...) that detected issues

        Returns:
            The implement step being re-run, or None if failed
        """
        # Get fix instructions and context from trigger step outputs
        fix_instructions = trigger_step.outputs.get("fix_instructions", "")
        fix_context = trigger_step.outputs.get("fix_context", {})
        fix_needed = trigger_step.outputs.get("fix_needed", True)

        if not fix_needed:
            logger.warning("Fix transition called but fix_needed is False")
            return None

        # Find the implement step in history
        implement_step: Optional[Step] = None
        for step_id in reversed(flow.state.step_history):
            step = flow.state.steps.get(step_id)
            if step and step.step_type == StepType.IMPLEMENT:
                implement_step = step
                break

        if not implement_step:
            logger.warning("No implement step found for fix transition")
            return None

        # Increment fix iteration counter
        trigger_step_type = trigger_step.step_type.value
        iteration = flow.state.increment_fix_iteration(
            fix_context={
                "trigger_step_id": trigger_step.step_id,
                "trigger_step_type": trigger_step_type,
                "implement_step_id": implement_step.step_id,
                "reason": fix_context.get("reason") or _infer_fix_reason(trigger_step_type),
                "issues": _normalize_issue_fields(
                    copy.deepcopy(
                        _cap_issue_list(
                            fix_context.get("spec_issues") or fix_context.get("issues", [])
                        )
                    )
                ),
            }
        )

        # Capture before the pending IMPLEMENT is persisted or invoked. This is
        # the only recoverable anchor for the exact code delta attributable to
        # this fix attempt; a missing/corrupt capture is recorded as unavailable
        # and later forces a full review instead of masquerading as an empty diff.
        self._capture_fix_review_baseline(flow, iteration)

        logger.info(
            f"Transitioning to fix iteration {iteration} for {implement_step.step_type.value}"
        )

        # Mechanism B per-flow budget: only when this fix iteration is actually
        # targeting baseline (inherited) failures — signalled by a non-empty
        # ``baseline_failures_targeted`` in the trigger's fix_context — do we
        # charge the independent baseline budget. An introduced-only fix never
        # carries this key, so it does not consume the baseline budget. The
        # test step's run_and_classify_tests reads this counter back to enforce
        # the per-flow cap (kept distinct from the possibly-unlimited global
        # max_fix_iterations).
        if fix_context.get("baseline_failures_targeted"):
            prior = flow.state.context.get("baseline_fix_attempts", 0)
            flow.state.context["baseline_fix_attempts"] = prior + 1
            logger.info(
                "Mechanism B: baseline_fix_attempts incremented to %d (targeting %d baseline failure(s))",
                flow.state.context["baseline_fix_attempts"],
                len(fix_context.get("baseline_failures_targeted") or []),
            )

        # Clear any Phase 1 cache — fix loop means a full fresh LLM call
        clear_phase1_cache(self.project_root, flow.flow_id, implement_step.step_id)

        # Update the existing implement step for the fix iteration
        implement_step.status = StepStatus.PENDING
        # Use the effective task_description (refined + interjections) so
        # fix iterations see the same task content downstream steps see.
        # Reverting to ``flow.task_description`` here would drop any
        # discovery-refined wording and any Ctrl-C user interjections —
        # making mid-flow corrections invisible to every fix iteration.
        implement_step.inputs["task_description"] = _compose_effective_task_description(flow)
        # Same reasoning for the root-cause report: this path REUSES the
        # implement step instead of rebuilding its inputs, so without an
        # explicit copy here the fix iterations would be the only implement
        # calls blind to the investigation that motivated the whole fix.
        # It stays a dedicated key — the intent-chain fields above
        # (``task_description``, ``task_groups``) are untouched by it, keeping
        # self_check's verbatim-quote source pool free of report text.
        investigation_report = _latest_investigation_report(flow)
        if investigation_report:
            implement_step.inputs["root_cause_report"] = copy.deepcopy(
                investigation_report
            )
            if flow.state.context.get("investigation_exhausted"):
                implement_step.inputs["investigation_exhausted"] = True
        implement_step.inputs["fix_instructions"] = fix_instructions
        implement_step.inputs["fix_context"] = fix_context
        implement_step.inputs["is_fix_iteration"] = True
        implement_step.inputs["fix_iteration"] = iteration
        # A fix iteration is a NEW LLM call with its own FIX_PROMPT, not a
        # retry of the prior implement call. Clear any stale retry counter
        # so LLMCaller doesn't discard the fix prompt via retry-context.
        _reset_retry_counter_for_new_call(implement_step)

        # Serialize previous outputs for reference. Exclude internal keys and
        # any nested previous_output to prevent quadratic growth across iterations.
        # Cap serialized size so a runaway output can't blow the next prompt.
        prev_outputs = {
            k: v for k, v in implement_step.outputs.items()
            if not k.startswith("_") and k != "previous_output"
        }
        if prev_outputs:
            try:
                serialized = json.dumps(prev_outputs, default=str)
                if len(serialized) > _PREVIOUS_OUTPUT_MAX_BYTES:
                    logger.info(
                        "previous_output truncated from %d to %d bytes",
                        len(serialized), _PREVIOUS_OUTPUT_MAX_BYTES,
                    )
                    implement_step.inputs["previous_output"] = {
                        "_truncated": True,
                        "_original_size": len(serialized),
                        "preview": serialized[:_PREVIOUS_OUTPUT_MAX_BYTES],
                    }
                else:
                    implement_step.inputs["previous_output"] = json.loads(serialized)
            except Exception:
                logger.warning("Failed to serialize previous outputs", exc_info=True)
                implement_step.inputs["previous_output"] = {}
        prev_changes = implement_step.outputs.get("changes_made", {})
        if prev_changes:
            implement_step.inputs["previous_changes"] = prev_changes

        # Point flow back to the implement step
        flow.state.current_step_id = implement_step.step_id

        # Find the index of IMPLEMENT in selected_steps
        try:
            step_index = flow.state.selected_steps.index(StepType.IMPLEMENT)
            flow.state.current_step_index = step_index
        except ValueError:
            logger.warning("IMPLEMENT step type not in selected sequence")

        self.persistence.save_flow(flow)

        print(f"\n{'='*60}")
        print(t("engine.fixloop.banner_title"))
        print(f"{'='*60}")
        print(t("engine.fixloop.iteration", iteration=iteration))
        if fix_context.get("test_failed"):
            print(t("engine.fixloop.reason_tests_failed"))
        if trigger_step_type == "self_check":
            print(t("engine.fixloop.source_self_check"))
        if trigger_step_type == "invariant_check":
            print(t("engine.fixloop.source_invariant_check"))
        if trigger_step_type == "verify_spec":
            print(t("engine.fixloop.source_verify_spec"))
        if fix_context.get("reason") == "self_check":
            print(t("engine.fixloop.reason_self_check"))
        if fix_context.get("spec_issues"):
            print(t("engine.fixloop.reason_spec_issues"))
        shown = (
            f"{fix_instructions[:200]}..."
            if len(fix_instructions) > 200
            else fix_instructions
        )
        print(t("engine.fixloop.instructions", instructions=shown))
        print(f"{'='*60}\n")

        return implement_step

    def _maybe_transition_to_adjudicate(
        self,
        flow: FlowInstance,
        trigger_step: Step,
        current_iteration: int,
    ) -> Optional[Step]:
        """Evaluate the oscillation triggers on a SELF_CHECK REVISION_NEEDED.

        Structural trigger evaluation only — the truth verdict (is a candidate
        oscillation a *real* spec contradiction?) is delegated to the ADJUDICATE
        step's LLM. When any trigger fires (candidate oscillation / 打脸 /
        reproduction, or the periodic backstop), routes to a dynamically-inserted
        ADJUDICATE step via ``_transition_to_adjudicate`` and returns it;
        otherwise returns ``None`` so the caller falls back to the normal
        ``_transition_to_fix``.

        The caller MUST gate this on ``trigger_step`` being SELF_CHECK: only
        SELF_CHECK records into the ledger, so TEST / INVARIANT_CHECK have no
        history to evaluate and must keep their fix routing unchanged.
        """
        issues = trigger_step.outputs.get("issues", []) or []
        # ``adjudicate_period`` is the periodic backstop (every N fix iterations).
        # Degrade to 0 (disabled) on any config error so a malformed yaml never
        # crashes a transition — the structural signals still fire.
        try:
            period_n = self._get_workflow_config().adjudicate_period
        except Exception:
            logger.warning(
                "Failed to read workflow.adjudicate_period; periodic backstop "
                "disabled for this transition", exc_info=True,
            )
            period_n = 0

        try:
            decision = adjudication.evaluate_triggers(
                flow.state.context, issues, current_iteration, period_n=period_n,
            )
        except Exception:
            logger.warning(
                "Adjudication trigger evaluation failed; falling back to the "
                "normal fix loop", exc_info=True,
            )
            return None

        if not decision.triggered:
            return None

        logger.info(
            "Adjudication triggered (reasons=%s) at fix iteration %d; routing to "
            "ADJUDICATE instead of the fix loop",
            decision.reasons, current_iteration,
        )
        return self._transition_to_adjudicate(
            flow, trigger_step, current_iteration, decision,
        )

    def _transition_to_adjudicate(
        self,
        flow: FlowInstance,
        trigger_step: Step,
        current_iteration: int,
        decision: "adjudication.AdjudicationDecision",
    ) -> Optional[Step]:
        """Insert an ADJUDICATE step ahead of the current SELF_CHECK slot.

        ADJUDICATE is a dynamically-inserted step (like the fix loop's re-entry
        into implement). It is inserted immediately *before* the SELF_CHECK slot
        the trigger step occupies so that, once it completes, normal progression
        re-runs SELF_CHECK — skipping IMPLEMENT/TEST, since the ruling changed no
        code. The new step and the mutated ``selected_steps`` both round-trip
        through persistence, so a ``--resume`` can recover at the ADJUDICATE
        break point (``StepType.ADJUDICATE`` is a first-class step type).

        Returns the ADJUDICATE step, or ``None`` if SELF_CHECK is somehow absent
        from the sequence (the caller then falls back to the fix loop).
        """
        selected = flow.state.selected_steps
        # Prefer the live ``current_step_index`` when it already points at a
        # SELF_CHECK slot (so a re-adjudication inserts ahead of the *current*
        # SELF_CHECK, not the first one in a sequence that already carries an
        # earlier inserted ADJUDICATE). Otherwise locate the first SELF_CHECK.
        cur = flow.state.current_step_index
        if 0 <= cur < len(selected) and selected[cur] == StepType.SELF_CHECK:
            insert_index = cur
        else:
            try:
                insert_index = selected.index(StepType.SELF_CHECK)
            except ValueError:
                logger.warning(
                    "SELF_CHECK not in selected sequence; cannot route to "
                    "ADJUDICATE, falling back to fix loop"
                )
                return None

        selected.insert(insert_index, StepType.ADJUDICATE)

        inputs = self._build_step_inputs(flow, StepType.ADJUDICATE)
        # Surface the structural trigger result to the handler under the FLAT keys
        # it actually reads (``adjudication_triggering_positions`` /
        # ``adjudication_reasons``): the prompt renders the real trigger reasons
        # and the handler unions the explicit positions into its candidate list.
        # A reproduction-only trigger yields a fingerprint whose position may have
        # a single expected value (so the ≥2-distinct candidate scan would miss
        # it); fold each triggering fingerprint back to its position_key so that
        # position is still presented to the LLM for a ruling. The full
        # cross-round ledger lives on ``flow.state.context`` and is read there.
        positions = list(decision.triggering_positions)
        for fp in decision.triggering_fingerprints:
            pk = fp.rsplit(adjudication._KEY_SEP, 1)[0]
            if pk not in positions:
                positions.append(pk)
        inputs["adjudication_triggering_positions"] = positions
        inputs["adjudication_reasons"] = list(decision.reasons)
        # Kept for audit/history (structured trigger detail); not read by the
        # handler, which consumes the flat keys above.
        inputs["adjudication_decision"] = {
            "reasons": list(decision.reasons),
            "triggering_positions": list(decision.triggering_positions),
            "triggering_fingerprints": list(decision.triggering_fingerprints),
            "details": copy.deepcopy(decision.details),
        }
        inputs["fix_iteration"] = current_iteration
        # Record which SELF_CHECK triggered this ruling. Ruling routing is now
        # gated two-phase: a real-contradiction ruling supersedes and reflows to a
        # fresh SELF_CHECK pass #1 (this trigger step is not re-entered — the link
        # is audit/history), while a no-op (review_divergence) ruling reads this
        # id to route straight back to the triggering SELF_CHECK's untouched
        # fix_instructions in IMPLEMENT — so for the no-op path it is load-bearing,
        # not audit-only.
        inputs["adjudication_trigger_step_id"] = trigger_step.step_id
        # Hand the triggering SELF_CHECK's pending fix_instructions to the
        # handler so a real-contradiction ruling can record them as superseded
        # (audit): its reflow drops them unimplemented, but the record of *what*
        # was dissolved must survive. A no-op ruling leaves them untouched and the
        # no-op routing re-feeds them into IMPLEMENT verbatim. Without this the
        # supersede audit would always be empty in the live path (only pre-seeded
        # test inputs carried it before).
        inputs["fix_instructions"] = trigger_step.outputs.get("fix_instructions", "") or ""

        step = Step(
            step_type=StepType.ADJUDICATE,
            status=StepStatus.PENDING,
            inputs=inputs,
        )
        flow.state.add_step(step)
        flow.state.current_step_id = step.step_id
        flow.state.current_step_index = insert_index

        # Anchor the periodic backstop to this adjudication so the next periodic
        # sweep measures elapsed fix iterations from here, not from flow start.
        try:
            adjudication.note_adjudication_ran(flow.state.context, current_iteration)
        except Exception:
            logger.warning(
                "Failed to reset adjudication period baseline", exc_info=True
            )

        self.persistence.save_flow(flow)

        print(f"\n{'='*60}")
        print(t("engine.adjudicate.banner_title"))
        print(f"{'='*60}")
        print(
            t(
                "engine.adjudicate.trigger_reasons",
                reasons=", ".join(decision.reasons) or t("engine.adjudicate.none"),
            )
        )
        print(t("engine.adjudicate.fix_iteration", iteration=current_iteration))
        print(t("engine.adjudicate.source_oscillation"))
        print(f"{'='*60}\n")

        return step

    def _maybe_confirm_adjudication(
        self,
        flow: FlowInstance,
        adjudicate_step: Step,
    ) -> Optional[Step]:
        """Gate an ADJUDICATE ruling behind the confirmation门 when required.

        Adjudicate is a **standard opt-in** confirmation step: like every other
        non-plan step, it is confirmed **iff** it is registered under
        ``confirmation.steps.adjudicate``. When the entry is absent
        (``resolve_confirm_inputs`` → ``None``), *any* ruling — including one
        that rewrites the **task description** — auto-passes with no CONFIRM. The
        trade-off is deliberate: the default is unattended-friendly, so a
        contradiction ruling may silently rewrite the task description; opt in to
        ``adjudicate: {reviewer: human}`` when that rewrite must be human-gated.

        When the entry *is* present, the reviewer decides: reviewer=human →
        PAUSED / luo calls; an LLM reviewer → synchronous review. One carve-out
        survives the flipped default: a ruling that only overrides the **plan /
        test expectations** (no description rewrite) is *never* human-gated even
        when explicitly configured ``reviewer: human`` — human review guards
        high-impact description rewrites, and pausing an unattended run for a
        mere plan tweak is exactly what the spec forbids. A plan-only ruling thus
        inserts a CONFIRM only under an explicit **LLM** reviewer. A benign
        ruling (no override patch at all) needs no confirmation.

        Returns the inserted CONFIRM step (routing the flow to approval) when a
        gate is required and not yet satisfied, or ``None`` so the caller reflows
        straight to SELF_CHECK. Idempotent across a re-entry: once an *approved*
        CONFIRM has reviewed this same ADJUDICATE step, it returns ``None``.
        """
        outputs = adjudicate_step.outputs
        desc_changed = bool(outputs.get("adjudicated_description"))
        plan_changed = bool(outputs.get("adjudicated_plan"))
        if not (desc_changed or plan_changed):
            return None

        # Already approved for this ruling? Then the gate is cleared. (A
        # non-approved CONFIRM does not count — its revision re-ran ADJUDICATE,
        # producing a fresh ruling that must be re-confirmed.)
        for sid in flow.state.step_history:
            s = flow.state.steps.get(sid)
            if s and s.step_type == StepType.CONFIRM:
                rr = s.outputs.get("review_result", {})
                if (
                    rr.get("step_to_review_id") == adjudicate_step.step_id
                    and rr.get("approved") is True
                ):
                    return None

        # Resolve the reviewer to decide whether a gate applies. Adjudicate is
        # opt-in: an absent entry means auto-pass, so a resolve failure defaults
        # the same way (auto-pass + warning) rather than reviving the old human
        # fallback — keeping the default consistent even on the exception edge.
        try:
            resolved = resolve_confirm_inputs(
                self.project_root, StepType.ADJUDICATE.value,
            )
        except Exception:
            logger.warning(
                "Failed to resolve adjudicate confirmation config; "
                "auto-passing the ruling (no confirmation)", exc_info=True,
            )
            return None

        # Not registered under confirmation.steps → auto-pass. This is the new
        # default: adjudicate rulings (including description rewrites) take
        # effect with no human/LLM门 unless the step is explicitly opted in.
        if resolved is None:
            return None

        # A plan-only ruling is never human-gated even when configured
        # ``reviewer: human`` — human review guards *description rewrites* (the
        # high-impact act), and pausing an unattended run for a mere plan /
        # test-expectation override is the exact behaviour the spec forbids.
        # Only a non-human (LLM) reviewer inserts a CONFIRM for a plan-only ruling.
        if not desc_changed and resolved.get("reviewer") == "human":
            return None

        return self._insert_adjudicate_confirm(flow, adjudicate_step)

    def _insert_adjudicate_confirm(
        self,
        flow: FlowInstance,
        adjudicate_step: Step,
    ) -> Step:
        """Insert a CONFIRM step reviewing the ADJUDICATE ruling.

        Placed immediately after the ADJUDICATE slot (before the SELF_CHECK the
        ruling re-runs). ``_build_step_inputs`` resolves the reviewer from
        ``confirmation.steps.adjudicate`` (the most-recent unconfirmed non-confirm
        step is this ADJUDICATE), so a human reviewer PAUSEs on a tianluo/calls file
        and an LLM reviewer runs synchronously — reusing confirm.py wholesale, no
        new counter. A rejected review flows back through the shared
        ``_transition_to_revision`` path (review_iterations-bounded) to re-run
        ADJUDICATE with the reviewer's feedback.
        """
        selected = flow.state.selected_steps
        cur = flow.state.current_step_index
        insert_at = cur + 1 if 0 <= cur < len(selected) else len(selected)
        selected.insert(insert_at, StepType.CONFIRM)

        inputs = self._build_step_inputs(flow, StepType.CONFIRM)
        confirm_step = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.PENDING,
            inputs=inputs,
        )
        flow.state.add_step(confirm_step)
        flow.state.current_step_id = confirm_step.step_id
        flow.state.current_step_index = insert_at

        self.persistence.save_flow(flow)

        print(f"\n{'='*60}")
        print(t("engine.adjudicate.confirm_title"))
        print(f"{'='*60}")
        print(t("engine.adjudicate.reviewer", reviewer=inputs.get("reviewer", "human")))
        print(f"{'='*60}\n")

        return confirm_step

    def _strip_inserted_adjudicate_run(
        self, selected: list, from_index: int
    ) -> Optional[int]:
        """Remove the dynamically-inserted ADJUDICATE + trailing CONFIRM slots.

        These transient slots sit immediately before the reflowed SELF_CHECK
        (ADJUDICATE inserted before SELF_CHECK; a confirmation门 CONFIRM, if any,
        between them). The post-ADJUDICATE reflow — uniform now across patch and
        no-op (review_divergence) rulings — must clear them, or the next fix-loop
        cycle's sequential progression after TEST enters a leftover slot: a stale
        CONFIRM resolves 'the most recent unconfirmed non-confirm step' to TEST and
        PAUSEs the flow on a spurious human approval of a TEST step (re-pausing
        every subsequent round), while a leftover ADJUDICATE re-runs un-triggered.
        A CONFIRM from an earlier rejected description-patch ruling that has since
        been re-ruled benign must be stripped too — hence the walk-back over the
        whole contiguous CONFIRM/ADJUDICATE run rather than just the ADJUDICATE.

        Locates the first SELF_CHECK at/after ``from_index`` (falling back to the
        first anywhere), then walks back removing the contiguous CONFIRM/ADJUDICATE
        run, stopping once the ADJUDICATE is popped. Returns the SELF_CHECK index
        after the removals, or ``None`` (nothing removed) if no SELF_CHECK slot
        exists.
        """
        sc_index: Optional[int] = None
        for i in range(max(from_index, 0), len(selected)):
            if selected[i] == StepType.SELF_CHECK:
                sc_index = i
                break
        if sc_index is None:
            try:
                sc_index = selected.index(StepType.SELF_CHECK)
            except ValueError:
                return None
        j = sc_index - 1
        while j >= 0 and selected[j] in (StepType.ADJUDICATE, StepType.CONFIRM):
            is_adjudicate = selected[j] == StepType.ADJUDICATE
            selected.pop(j)
            sc_index -= 1
            if is_adjudicate:
                break
            j -= 1
        return sc_index

    def _transition_after_adjudicate(
        self,
        flow: FlowInstance,
        adjudicate_step: Step,
    ) -> Optional[Step]:
        """Reflow after an ADJUDICATE ruling lands: re-run SELF_CHECK at pass #1.

        The ruling changed the spec, not the code, so this skips IMPLEMENT/TEST
        entirely and re-runs SELF_CHECK against the now-adjudicated effective
        text (``_build_step_inputs`` switches the verbatim-quote source pool and,
        at pass #1, resets the deferred stash). The pending fix_instructions from
        the triggering SELF_CHECK are superseded — recorded in the ADJUDICATE
        outputs for audit and never fed to IMPLEMENT. No synthetic "description
        changed" issue is created: IMPLEMENT picks up the new text naturally via
        ``_effective_task_description_base`` if a still-valid issue routes it
        there.

        The re-run is counted as a fix iteration so ``max_fix_iterations`` keeps
        bounding a ruling that fails to converge (else an ineffective adjudication
        could oscillate forever without ever advancing the counter). Returns the
        fresh SELF_CHECK step, or ``None`` if no SELF_CHECK slot exists (the
        caller then falls through to normal progression).

        This is the real-contradiction (override-patch) reflow path. A no-op
        ruling (``review_divergence`` — no override patch) does NOT come here in
        the normal case: it is routed by ``_transition_after_adjudicate_noop``
        straight to IMPLEMENT with the triggering round's fix_instructions
        untouched (no supersede, no SELF_CHECK reflow, no extra fix iteration), so
        the effect is as if ADJUDICATE was never inserted. This method remains the
        no-op path's DEFENSIVE fallback only — reached when that routing cannot
        locate the triggering SELF_CHECK — so it must still degrade safely; its
        ledger effects (benign-candidate rejections) are applied below either way.
        """
        # Strip the dynamically-inserted ADJUDICATE slot AND its (optional)
        # CONFIRM slot — they sit immediately before the reflowed SELF_CHECK
        # (ADJUDICATE inserted before SELF_CHECK, CONFIRM between them). Every
        # ruling (patch or no-op) reflows identically via
        # ``_strip_inserted_adjudicate_run`` so their cleanup can never diverge
        # (see that helper for why the leftover slots must go).
        selected = flow.state.selected_steps
        cur = flow.state.current_step_index
        sc_index = self._strip_inserted_adjudicate_run(selected, cur)
        if sc_index is None:
            logger.warning(
                "SELF_CHECK slot missing after ADJUDICATE; cannot reflow"
            )
            return None

        # Apply the ruling's DEFERRED ledger side effects now that it has landed
        # (this reflow point is reached only after the confirmation门 approved,
        # or when免确认). A rejected ruling never reaches here, so its staged
        # abolish/reject effects never touch the persisted ledger. Applied before
        # the SELF_CHECK re-run so the abolished entries do not count toward the
        # next round's triggers.
        try:
            from .steps.adjudicate import apply_landed_ledger_effects
            apply_landed_ledger_effects(adjudicate_step, flow.state.context)
        except Exception:
            logger.warning(
                "Failed to apply adjudication ledger effects on landing",
                exc_info=True,
            )

        # Count the reflow as a fix iteration BEFORE building the SELF_CHECK
        # inputs so the pass sees ``fix_iteration > 0`` (which drives the source-
        # pool switch and prev-issue injection) and the global bound advances.
        iteration = flow.state.increment_fix_iteration(
            fix_context={
                "trigger_step_id": adjudicate_step.step_id,
                "trigger_step_type": StepType.ADJUDICATE.value,
                "reason": "adjudication_reflow",
            }
        )

        # The ruling changed the effective requirement authority without
        # changing code. Any in-flight incremental baseline therefore describes
        # the wrong review contract and must be discarded before pass #1.
        self._self_check_round_controller(flow).force_full(
            "effective_requirements_changed"
        )

        inputs = self._build_step_inputs(flow, StepType.SELF_CHECK)
        self_check_step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=inputs,
        )
        flow.state.add_step(self_check_step)
        flow.state.current_step_id = self_check_step.step_id
        flow.state.current_step_index = sc_index

        self.persistence.save_flow(flow)

        print(f"\n{'='*60}")
        print(t("engine.adjudicate.landed_title"))
        print(f"{'='*60}")
        print(t("engine.adjudicate.fix_iteration", iteration=iteration))
        print(t("engine.adjudicate.landed_skipped"))
        if adjudicate_step.outputs.get("fix_instructions_superseded"):
            print(t("engine.adjudicate.landed_superseded"))
        print(f"{'='*60}\n")

        return self_check_step

    def _transition_after_adjudicate_noop(
        self,
        flow: FlowInstance,
        adjudicate_step: Step,
    ) -> Optional[Step]:
        """Transparent pass-through after a no-op (review_divergence) ruling.

        The ruling found no real spec contradiction, so ADJUDICATE must behave as
        if it had never been inserted: no supersede, no SELF_CHECK reflow at pass
        #1, no extra fix-iteration increment. Instead we route the flow straight
        into IMPLEMENT via the SAME ``_transition_to_fix`` path a plain
        REVISION_NEEDED SELF_CHECK would take, feeding the triggering round's
        untouched ``fix_instructions``/``fix_context``. Reusing that path (rather
        than rebuilding the IMPLEMENT inputs here) precisely reproduces the single
        fix-iteration increment and instruction hand-off of the un-adjudicated
        flow, and keeps the two entry points from drifting.

        Exactly two mechanical bookkeeping effects survive the no-op (per the
        gated two-phase design): the inserted ADJUDICATE slot is stripped, and the
        handler-staged benign ``rejected_positions`` are landed into the ledger via
        ``apply_landed_ledger_effects`` (``abolished_fingerprints`` is empty here,
        so ``mark_abolished`` is skipped) — this is the trigger-layer filter that
        stops the same benign flip re-invoking the LLM every round. The
        ``period_baseline`` is NOT touched here: ``_transition_to_adjudicate``
        already reset it via ``note_adjudication_ran`` at insertion time.

        Returns the reused IMPLEMENT step. If the triggering SELF_CHECK cannot be
        resolved (or ``_transition_to_fix`` declines), defensively falls back to
        ``_transition_after_adjudicate`` so the flow always advances.
        """
        # Resolve the triggering SELF_CHECK first: the no-op reuses its pending
        # fix_instructions, so without it there is nothing to route into IMPLEMENT.
        trigger_id = adjudicate_step.inputs.get("adjudication_trigger_step_id")
        trigger_sc = flow.state.steps.get(trigger_id) if trigger_id else None
        if not (trigger_sc and trigger_sc.step_type == StepType.SELF_CHECK):
            logger.warning(
                "Adjudication no-op could not resolve its triggering SELF_CHECK "
                "(id=%s); falling back to the standard reflow to guarantee "
                "forward progress", trigger_id,
            )
            return self._transition_after_adjudicate(flow, adjudicate_step)

        # Strip the dynamically-inserted ADJUDICATE slot (a no-op never carries a
        # patch, so it never inserted a CONFIRM) — same cleanup the patch reflow
        # does, or a leftover slot would re-enter on the next sequential pass.
        selected = flow.state.selected_steps
        cur = flow.state.current_step_index
        self._strip_inserted_adjudicate_run(selected, cur)

        # Land the benign rejected_positions (only mechanical ledger effect the
        # no-op keeps). Idempotent, so a --resume re-entry is safe.
        try:
            from .steps.adjudicate import apply_landed_ledger_effects
            apply_landed_ledger_effects(adjudicate_step, flow.state.context)
        except Exception:
            logger.warning(
                "Failed to land benign rejected_positions on adjudication no-op",
                exc_info=True,
            )

        # Re-enter IMPLEMENT exactly as an un-adjudicated SELF_CHECK
        # REVISION_NEEDED would: one fix-iteration increment, triggering round's
        # instructions/context carried through.
        fix_step = self._transition_to_fix(flow, trigger_sc)
        if fix_step:
            print(f"\n{'='*60}")
            print(t("engine.adjudicate.noop_title"))
            print(f"{'='*60}")
            print(t("engine.adjudicate.noop_detail"))
            print(f"{'='*60}\n")
            return fix_step

        logger.warning(
            "Adjudication no-op fix transition returned None; falling back to the "
            "standard reflow to guarantee forward progress"
        )
        return self._transition_after_adjudicate(flow, adjudicate_step)

    def _get_max_fix_iterations(self) -> int:
        """Get the maximum number of fix iterations allowed.

        Returns:
            Maximum fix iterations (defaults to
            ``config.DEFAULT_MAX_FIX_ITERATIONS``). A return value of 0 is
            the sentinel for "unlimited". Negatives are rejected at config
            load time, so callers can assume the result is always ``>= 0``.
        """
        return self._get_workflow_config().max_fix_iterations

    def _get_workflow_config(self) -> "WorkflowConfig":
        """Load and cache workflow configuration for the current transition.

        Memoized on the instance to avoid re-reading tianluo.yaml within a
        single transition cycle. The cache is invalidated at the top of
        ``transition_to_next`` so each transition starts fresh.

        ``IOError``/``OSError``/``ImportError`` are caught defensively
        and degrade to defaults. ``load_project_yaml`` is documented as
        never-raising, but a future loader regression that lets an
        OS-level error escape — or a partial install / test mock that
        breaks the ``WorkflowConfig`` import path at runtime — should
        not crash the flow mid-transition; the fix loop is more useful
        with conservative defaults than with a hard failure.

        ``ConfigError`` (e.g. a mid-flow yaml hot-edit to a negative
        ``max_fix_iterations``) is caught here ONLY when a previously-
        loaded config is cached on the instance via
        ``_workflow_config_last_good``. Fail-fast applies whenever
        ``last_good is None``, which covers two cases:

        1. Startup via ``create_flow`` — the canonical fail-fast path.
           The user must fix tianluo.yaml before the flow starts.
        2. The first transition of a freshly resumed flow on a brand-new
           ``StateMachine`` instance (e.g. after process restart). The
           on-disk config is re-validated and an invalid value still
           halts the resume rather than running with silent defaults.
           This is intentional: a hot-edit to invalid yaml between the
           original run and the resume should be visible.

        After at least one successful load, a ``ConfigError`` from a
        subsequent hot-edit is logged and the flow continues on the
        last-known-good config — propagating the error out of a mid-flow
        transition would leave the flow ``RUNNING`` on disk because the
        persistence write happens before the exception, which is worse
        than continuing with stale-but-valid config.

        IMPORTANT: an ``IOError``/``OSError``/``ImportError`` on the
        very first load does NOT count as a successful load. The
        defaults used as a transient fallback are cached for the current
        transition only; ``_workflow_config_last_good`` stays ``None``
        so a subsequent ``ConfigError`` on the next transition still
        propagates. Otherwise an early IO race during ``create_flow``
        would silently promote defaults to "last good" and disable
        startup-style fail-fast for the rest of this StateMachine's
        lifetime.
        """
        cached = getattr(self, "_workflow_config_cache", None)
        if cached is not None:
            return cached
        last_good = getattr(self, "_workflow_config_last_good", None)
        try:
            cfg = WorkflowConfig.load(self.project_root)
        except (IOError, OSError, ImportError) as e:
            logger.warning(
                "Failed to load workflow config (%s); falling back to defaults", e
            )
            cfg = last_good if last_good is not None else WorkflowConfig()
            # Cache for this transition cycle only. Do NOT update
            # ``_workflow_config_last_good`` — IO/Import failure is not
            # a successful load, and treating fallback defaults as
            # last-known-good would let a later ConfigError be silently
            # swallowed.
            self._workflow_config_cache = cfg
            return cfg
        except ConfigError as e:
            if last_good is None:
                # No prior successful load on this StateMachine instance:
                # silently substituting defaults would mask a genuine
                # yaml error from the user. Re-raise. See the docstring
                # for the two cases this covers (startup and fresh
                # resume).
                raise
            logger.warning(
                "Workflow config became invalid mid-flow (%s); "
                "continuing with previously-loaded config to avoid "
                "leaving the flow in an inconsistent state. Fix "
                "tianluo.yaml to clear the warning.",
                e,
            )
            cfg = last_good
        self._workflow_config_cache = cfg
        self._workflow_config_last_good = cfg
        return cfg

    def _get_investigation_max_iterations(self) -> int:
        """Return the configured investigation round cap (0 = unlimited).

        Memoized per transition exactly like ``_get_workflow_config`` (the cache
        is cleared at the top of ``transition_to_next``) so a single transition
        parses tianluo.yaml at most once for this value.

        WHY a separate counter from ``max_fix_iterations``: an investigation
        round is an *exploration* budget, not a repair attempt. Sharing the fix
        counter would let a long repair history starve investigation — and
        would drag the investigation loop into the fix loop's REVISION_NEEDED
        routing, which it deliberately does not use (see
        ``_create_investigate_repeat_step``).

        Loader errors degrade to the default rather than crashing a transition;
        unlike the fix loop there is no fail-fast requirement here, because an
        unusable investigation cap cannot corrupt state — it only changes how
        many exploratory rounds run.
        """
        cached = getattr(self, "_investigation_config_cache", None)
        if cached is None:
            from ..config import InvestigationConfig

            try:
                cached = InvestigationConfig.load(self.project_root)
            except (ConfigError, IOError, OSError, ImportError) as e:
                logger.warning(
                    "Failed to load investigation config (%s); falling back to "
                    "defaults", e,
                )
                cached = InvestigationConfig()
            self._investigation_config_cache = cached
        return cached.max_iterations

    def _count_consecutive_investigate_completed(self, flow: FlowInstance) -> int:
        """Count consecutive COMPLETED investigate steps at the tail of step_history.

        Mirrors ``_count_consecutive_self_check_completed``: stops at the first
        step that is neither INVESTIGATE nor a COMPLETED CONFIRM, and at any
        INVESTIGATE whose status is not COMPLETED. CONFIRM is skipped because a
        project may gate ``investigate`` through ``confirmation.steps`` — the
        inserted CONFIRM must not break the round streak.

        Args:
            flow: Current flow instance.

        Returns:
            Number of consecutive COMPLETED INVESTIGATE steps at the tail
            (0 if none).
        """
        count = 0
        for step_id in reversed(flow.state.step_history):
            step = flow.state.steps.get(step_id)
            if not step:
                break
            if step.step_type == StepType.CONFIRM:
                if step.status == StepStatus.COMPLETED:
                    continue
                break
            if step.step_type != StepType.INVESTIGATE:
                break
            if step.status != StepStatus.COMPLETED:
                break
            count += 1
        return count

    def _create_investigate_repeat_step(self, flow: FlowInstance) -> Step:
        """Create the next round of the bounded investigation loop.

        INVARIANT: this loop must NEVER be expressed through REVISION_NEEDED.
        ``transition_to_next`` only honours REVISION_NEEDED for TEST / E2E /
        SELF_CHECK / INVARIANT_CHECK / VERIFY_SPEC; any other step type
        returning it falls straight through to the ordinary "advance to the
        next selected step" logic, so the loop would silently never run and
        nothing would report the failure. The repeat-step form (COMPLETED +
        a new Step at the same sequence slot) is the same one self_check's
        N-pass uses and is the only form the router actually implements.

        Args:
            flow: Current flow instance.

        Returns:
            A new PENDING INVESTIGATE Step, already added to flow.state.
        """
        inputs = self._build_step_inputs(flow, StepType.INVESTIGATE)

        step = Step(
            step_type=StepType.INVESTIGATE,
            status=StepStatus.PENDING,
            inputs=inputs,
        )
        flow.state.add_step(step)
        flow.state.current_step_id = step.step_id
        # current_step_index does NOT advance — the flow is still sitting on the
        # INVESTIGATE slot of selected_steps; only the round number moves.
        self.persistence.save_flow(flow)
        return step

    def _get_self_check_resolution(self):
        """Load and cache the resolved ``llm_caller.steps.self_check`` config.

        Memoized per transition (invalidated at the top of
        ``transition_to_next``) so the nested-chain count is read at most
        once per cycle. Degrades to the ``default`` form on any loader
        error so a malformed YAML never crashes a transition.
        """
        cached = getattr(self, "_self_check_resolution_cache", None)
        if cached is not None:
            return cached
        from ..config import SelfCheckResolution, load_self_check_resolution
        try:
            resolution = load_self_check_resolution(self.project_root)
        except ValueError:
            # Unknown agent name in the chain is a genuine config error and
            # must fail fast at the construction path (LLMCaller). Here, in
            # the pass-count derivation path, degrade to the default form so
            # the count still resolves; the real fail-fast surfaces when the
            # self_check LLMCaller is built.
            resolution = SelfCheckResolution(form="default")
        except (IOError, OSError, ImportError):
            resolution = SelfCheckResolution(form="default")
        self._self_check_resolution_cache = resolution
        return resolution

    def _get_self_check_passes_required(self) -> int:
        """Return the effective self_check pass count for the current config.

        Derivation:
        - When ``llm_caller.steps.self_check`` is a nested per-pass chain
          AND ``workflow.self_check_passes_required`` was NOT set
          explicitly, the effective count is the number of declared chains
          (the nested chain alone fully expresses the intent).
        - When both are set explicitly, ``self_check_passes_required``
          wins. If it is smaller than the chain count, a one-shot WARNING
          notes that the extra chains will not run.
        - Otherwise (flat / no self_check override), the configured
          ``self_check_passes_required`` (explicit or default 1) is used.
        """
        from ..config import effective_self_check_passes_required

        cfg = self._get_workflow_config()
        resolution = self._get_self_check_resolution()
        # One-shot WARNING when an explicit count is smaller than the chain
        # count (extra chains will not run). The effective count itself is
        # computed by the shared helper so the state machine and ``luo history
        # show`` can never disagree on the ``#i/N`` denominator.
        if (
            resolution.form == "nested"
            and cfg.self_check_passes_required_explicit
            and cfg.self_check_passes_required < resolution.chain_count
        ):
            self._warn_self_check_passes_below_chains(
                resolution.source_label,
                cfg.self_check_passes_required,
                resolution.chain_count,
            )
        return effective_self_check_passes_required(cfg, resolution)

    def _warn_self_check_passes_below_chains(
        self, source_label, passes: int, chain_count: int,
    ) -> None:
        """One-shot WARNING when explicit pass count < declared chain count."""
        warned = getattr(self, "_warned_self_check_passes_below_chains", None)
        if warned is None:
            warned = set()
            self._warned_self_check_passes_below_chains = warned
        key = (source_label, passes, chain_count)
        if key in warned:
            return
        warned.add(key)
        logger.warning(
            "workflow.self_check_passes_required=%d is smaller than the "
            "%d self_check chains declared in llm_caller.steps.self_check "
            "(%s); the last %d chain(s) will not be used.",
            passes, chain_count, source_label, chain_count - passes,
        )

    def _count_consecutive_self_check_completed(self, flow: FlowInstance) -> int:
        """Count consecutive COMPLETED self_check steps from the end of step_history.

        Stops at the first non-self_check step (other than CONFIRM) or any
        self_check with a status other than COMPLETED. CONFIRM steps are skipped
        because they can be inserted between self_check and the next step in the
        sequence; their presence must not break the pass streak.

        Args:
            flow: Current flow instance.

        Returns:
            Number of consecutive COMPLETED self_check steps at the tail of
            step_history (0 if none).
        """
        count = 0
        for step_id in reversed(flow.state.step_history):
            step = flow.state.steps.get(step_id)
            if not step:
                break
            if step.step_type == StepType.CONFIRM:
                if step.status == StepStatus.COMPLETED:
                    continue
                break
            if step.step_type != StepType.SELF_CHECK:
                break
            if step.status != StepStatus.COMPLETED:
                break
            count += 1
        return count

    def _unflushed_deferred_issues(
        self, flow: FlowInstance, step: Step
    ) -> list:
        """Deferred findings the terminal pass never consumed into a fix loop.

        The handler always writes ``self_check_deferred_issues`` back (``[]``
        once flushed), so an outputs-less terminal pass — the Skip gate
        force-completing a FAILED step — is the case where the context stash
        survives with nobody to consume it.

        WHY the guard is per ROUND, not per flow: it exists only to stop a
        repeatedly skipped pass from looping inside one round. A later round
        stashes its own freshly validated findings, and those must still get
        their rescue — a flow-wide latch would silently discard them, the one
        outcome the check-step contract forbids. The CALLER checks the
        skipped-rescue case against the stash before consulting this latch,
        so an attempted-but-unconsumed stash routes into the fix loop
        instead of being latched away here.
        """
        attempted = flow.state.context.get("self_check_deferred_flush_attempted")
        round_id = self._deferred_flush_round_key(step)
        if isinstance(attempted, str) and attempted and attempted == round_id:
            return []
        if step.outputs.get("fix_needed"):
            return []
        if "self_check_deferred_issues" in (step.outputs or {}):
            return []
        stash = flow.state.context.get("self_check_deferred_issues") or []
        return list(stash) if isinstance(stash, list) else []

    @staticmethod
    def _deferred_flush_round_key(step: Step) -> str:
        """Identity of the round a deferred-flush rescue belongs to.

        Legacy rounds carry no scope metadata, so they share one key — the
        rescue stays bounded for them exactly as before.
        """
        return str(step.inputs.get("self_check_round_id") or "") or "legacy-round"

    def _route_deferred_into_fix_loop(
        self, flow: FlowInstance, step: Step, issues: list
    ) -> None:
        """Attach unflushed deferred findings to the step as fix-loop outputs.

        Mirrors the handler's ``_build_fix_outputs`` shape so
        ``_transition_to_fix`` consumes them exactly like a normal
        finding-bearing pass: ``fix_instructions`` for the implement prompt,
        ``fix_context.issues`` for the fix-iteration record, and a cleared
        stash so a downstream reader cannot mistake them for still-pending
        deferrals.
        """
        issue_details = "\n".join(
            f"- [{str(issue.get('severity', 'high'))}] "
            + " | ".join(
                [
                    str(issue.get("location") or "?"),
                    (
                        f"actual: "
                        f"{issue.get('actual_behavior') or issue.get('description') or ''}"
                    ),
                    f"expected: {issue.get('expected_behavior') or ''}",
                    f"divergence: {issue.get('divergence') or ''}",
                ]
            )
            for issue in issues
            if isinstance(issue, dict)
        )
        fix_iteration = flow.state.get_fix_iteration()
        step.outputs["issues"] = copy.deepcopy(issues)
        step.outputs["actionable_count"] = len(issues)
        step.outputs["fix_needed"] = True
        step.outputs["fix_iteration"] = fix_iteration
        step.outputs["max_fix_iterations"] = self._get_max_fix_iterations()
        step.outputs["fix_instructions"] = (
            f"Self-check found {len(issues)} deferred issue(s) that need "
            "fixing:\n"
            f"{issue_details}\n\n"
            "Fix the issues listed above and ensure the logic is correct."
        )
        step.outputs["fix_context"] = {
            "reason": "self_check",
            "issues": copy.deepcopy(issues),
            "iteration": fix_iteration + 1,
        }
        step.outputs["self_check_deferred_issues"] = []
        flow.state.context["self_check_deferred_issues"] = []

    def _create_self_check_repeat_step(
        self, flow: FlowInstance, *, advance_pass: bool = True
    ) -> Step:
        """Create a repeated self_check Step instance for the N-pass requirement.

        Builds inputs via _build_step_inputs which computes the pass position so
        the handler can log ``#i/N`` and the inputs carry the right metadata.

        Args:
            flow: Current flow instance.

        Returns:
            A new Step instance of type SELF_CHECK, already added to flow.state.
        """
        if advance_pass:
            self._self_check_round_controller(flow).advance_pass()
        inputs = self._build_step_inputs(flow, StepType.SELF_CHECK)

        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=inputs,
        )
        flow.state.add_step(step)
        flow.state.current_step_id = step.step_id
        # current_step_index does NOT advance — we are still at the SELF_CHECK
        # slot in the selected_steps sequence.
        self.persistence.save_flow(flow)
        return step

    def _maybe_reflow_self_check_for_requirements(
        self, flow: FlowInstance, current_step: Step
    ) -> Optional[Step]:
        """Re-enter full SELF_CHECK when requirements mutate after its slot."""
        if current_step.step_type == StepType.SELF_CHECK:
            return None
        review_state = flow.state.context.get("self_check_review")
        if not isinstance(review_state, dict) or not review_state.get(
            "force_full_reason"
        ):
            return None
        selected = flow.state.selected_steps
        try:
            self_check_index = selected.index(StepType.SELF_CHECK)
        except ValueError:
            return None
        current_index = flow.state.current_step_index
        if (
            current_index < len(selected)
            and selected[current_index] != current_step.step_type
        ):
            try:
                current_index = selected.index(current_step.step_type)
            except ValueError:
                return None
        if current_index < self_check_index:
            # The ordinary forward sequence will consume the force-full marker
            # when it reaches SELF_CHECK; no dynamic reflow is needed yet.
            return None

        inputs = self._build_step_inputs(flow, StepType.SELF_CHECK)
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=inputs,
        )
        flow.state.add_step(step)
        flow.state.current_step_id = step.step_id
        flow.state.current_step_index = self_check_index
        self.persistence.save_flow(flow)
        logger.info(
            "Effective requirements changed after SELF_CHECK; reflowing to "
            "full pass #1 before downstream gates may advance"
        )
        return step

    def _build_step_inputs(self, flow: FlowInstance, step_type: StepType) -> Dict[str, Any]:
        """Build inputs for a step based on previous outputs.

        Args:
            flow: Current flow instance
            step_type: Type of step to build inputs for

        Returns:
            Dictionary of inputs
        """
        inputs: Dict[str, Any] = {
            "task_description": flow.task_description,
            "flow_id": flow.flow_id,
        }

        # Root-cause reports of the completed investigation rounds, oldest first.
        # Held in a local (not written into ``inputs`` inside the loop) so the
        # report reaches ONLY the step types that are supposed to see it — see
        # the injection block near the end of this method.
        investigation_reports: List[Dict[str, Any]] = []

        # The doctrine/granularity PLAN recorded when it ran, harvested from its
        # own outputs. Held in a local so the forwarding block below can prefer
        # it over the flow context — see there for why the record wins.
        planned_mode: Dict[str, Any] = {}

        # Gather outputs from previous steps
        for step_id in flow.state.step_history:
            step = flow.state.steps.get(step_id)
            if step and step.status in (StepStatus.COMPLETED, StepStatus.PARTIAL):
                # Add key outputs based on step type
                if step.step_type == StepType.DISCOVERY:
                    inputs["refined_description"] = step.outputs.get("refined_description")
                    inputs["discovery_summary"] = step.outputs.get("discovery_summary")
                elif step.step_type == StepType.ANALYZE:
                    inputs["task_type"] = step.outputs.get("task_type")
                    inputs["scope"] = step.outputs.get("scope")
                    inputs["complexity"] = step.outputs.get("complexity")
                    inputs["analysis_reasoning"] = step.outputs.get("reasoning")
                    # Merged from the former project_summary step. Downstream
                    # steps receive the charter + code-index injection as their
                    # project-convention channel.
                    inputs["project_summary"] = step.outputs.get("project_summary")
                # Deprecated: PROJECT_SUMMARY merged into ANALYZE (backward compat for persisted flows)
                elif step.step_type == StepType.PROJECT_SUMMARY:
                    inputs["project_summary"] = step.outputs.get("project_summary")
                elif step.step_type == StepType.INVESTIGATE:
                    # COMPLETED only: a PARTIAL round carries no verdict, and a
                    # half-formed hypothesis must not be handed downstream as
                    # "the" root cause.
                    if step.status == StepStatus.COMPLETED:
                        report = step.outputs.get("root_cause_report")
                        if isinstance(report, dict) and report:
                            investigation_reports.append(report)
                elif step.step_type == StepType.PLAN:
                    # PLAN emits scheduling data only. The deprecated
                    # PROPOSE/DESIGN branches below still forward `proposal` /
                    # `design_doc` so a persisted legacy flow resumes unchanged.
                    inputs["task_groups"] = step.outputs.get("task_groups")
                    for _mode_key in (PLAN_DECOMPOSITION_KEY, PLAN_GRANULARITY_KEY):
                        _recorded = step.outputs.get(_mode_key)
                        if _recorded:
                            planned_mode[_mode_key] = _recorded
                # Deprecated step types (backward compat for persisted flows)
                elif step.step_type == StepType.PROPOSE:
                    inputs["proposal"] = step.outputs.get("proposal")
                elif step.step_type == StepType.DESIGN:
                    inputs["design_doc"] = step.outputs.get("design_doc")
                elif step.step_type == StepType.PLAN_TASKS:
                    inputs["task_groups"] = step.outputs.get("task_groups")
                elif step.step_type == StepType.IMPLEMENT:
                    # Build changes_made from implement outputs
                    inputs["changes_made"] = {
                        "files_changed": step.outputs.get("files_changed", []),
                        "implemented_groups": step.outputs.get("implemented_groups", []),
                    }
                    # Forward implement-test contract
                    inputs["tests_added"] = step.outputs.get("tests_added", [])
                    inputs["test_mapping"] = step.outputs.get("test_mapping", {})
                    inputs["estimated_test_duration"] = step.outputs.get("estimated_test_duration")
                    # Forward completion status and summary for downstream steps
                    inputs["implement_summary"] = step.outputs.get("summary", "")
                    inputs["completion_status"] = step.outputs.get("completion_status", "complete")
                    inputs["incomplete_tasks"] = step.outputs.get("incomplete_tasks", [])
                    # Forward restricted edits results for downstream visibility
                    inputs["restricted_edits_applied"] = step.outputs.get("restricted_edits_applied", [])
                    inputs["restricted_edits_failed"] = step.outputs.get("restricted_edits_failed", [])
                    # Forward pre-session version + session-introduced commits so
                    # version_analyze can discount any version-file edits already
                    # merged onto main during the implement (worktree) phase.
                    # Older persisted flows without these fields safely degrade
                    # to None / [] (version_analyze treats both as "no prior
                    # session bumps in play").
                    inputs["pre_session_version"] = step.outputs.get("pre_session_version")
                    inputs["session_commits"] = step.outputs.get("session_commits", [])
                elif step.step_type == StepType.TEST:
                    inputs["test_results"] = step.outputs.get("test_results")
                elif step.step_type == StepType.SELF_CHECK:
                    inputs["self_check_result"] = step.outputs.get("self_check_result")
                    inputs["self_check_issues"] = step.outputs.get("issues")
                elif step.step_type == StepType.VERSION_ANALYZE:
                    inputs["bump_type"] = step.outputs.get("bump_type")
                    inputs["reasoning"] = step.outputs.get("reasoning")
                    inputs["confidence"] = step.outputs.get("confidence")
                    inputs["suggested_version"] = step.outputs.get("suggested_version")
                    inputs["is_tag"] = step.outputs.get("is_tag")
                    inputs["commit_message"] = step.outputs.get("commit_message")
                    # Forward changelog bullets so the commit step's
                    # DocumentationUpdater wiring can write VERSIONS.md. Absent on
                    # older persisted flows -> [] (commit falls back to the commit
                    # message subject).
                    inputs["versions_changes"] = step.outputs.get("versions_changes", [])
                elif step.step_type == StepType.COMMIT:
                    inputs["commit_hash"] = step.outputs.get("commit_hash")
                elif step.step_type == StepType.CONFIRM:
                    # Pass through review result for tracking
                    inputs["last_review_result"] = step.outputs.get("review_result")

        # When discovery produced a refined_description, preserve original
        # for traceability and override the effective task_description.
        # Then apply any persisted user-Ctrl-C interjections.
        # ``_compose_effective_task_description`` encapsulates the same
        # priority chain (refined > original, then interjections) used by
        # ``_transition_to_fix`` so the two paths cannot diverge.
        if "refined_description" in inputs and inputs["refined_description"]:
            inputs["original_task_description"] = inputs["task_description"]
        inputs["task_description"] = _compose_effective_task_description(flow)

        if step_type in (StepType.PLAN, StepType.IMPLEMENT):
            # The doctrine/granularity a flow entered, forwarded so neither step
            # re-decides anything: PLAN emits under the doctrine the flow was
            # created with, and IMPLEMENT's execution shape follows from that
            # doctrine plus either the forced granularity or, when the group
            # count was left to PLAN, the count it already emitted.
            #
            # INVARIANT: PLAN's own record wins over the flow context. A flow
            # created before this model existed has no doctrine in its context
            # at all, so PLAN resolved one by projection and wrote it to its
            # outputs; feeding IMPLEMENT the (empty) context instead would let
            # it re-project independently and reach a different answer for the
            # very same plan — coarse capability groups judged by the granular
            # per-task contract they deliberately do not carry.
            for _mode_key in (PLAN_DECOMPOSITION_KEY, PLAN_GRANULARITY_KEY):
                inputs[_mode_key] = planned_mode.get(
                    _mode_key,
                ) or flow.state.context.get(_mode_key)

        if step_type == StepType.IMPLEMENT:
            inputs["analysis_context"] = {
                "scope": inputs.get("scope"),
                "complexity": inputs.get("complexity"),
                "reasoning": inputs.get("analysis_reasoning"),
                "project_summary": inputs.get("project_summary"),
            }

        # Special handling for CONFIRM step
        if step_type == StepType.CONFIRM:
            # Determine which step we're confirming
            # Find the most recent non-confirm step that hasn't been confirmed yet
            last_non_confirm_step = None
            for step_id in reversed(flow.state.step_history):
                step = flow.state.steps.get(step_id)
                if step and step.step_type != StepType.CONFIRM:
                    # Check if this step has been confirmed
                    already_confirmed = False
                    for sid in flow.state.step_history:
                        s = flow.state.steps.get(sid)
                        if s and s.step_type == StepType.CONFIRM:
                            review_result = s.outputs.get("review_result", {})
                            # Only an *approved* CONFIRM marks its reviewed
                            # step as confirmed. A CONFIRM that requested
                            # changes (approved is False / absent) must NOT
                            # shield its target step: after the revision
                            # re-runs that step, the next CONFIRM has to
                            # re-review the same step with the LLM instead of
                            # skipping forward to an unconfigured later step
                            # (which would otherwise hit the human fallback
                            # below). Strict 'is True' so a missing/false
                            # approved never counts.
                            if (
                                review_result.get("step_to_review_id") == step_id
                                and review_result.get("approved") is True
                            ):
                                already_confirmed = True
                                break
                    if not already_confirmed:
                        last_non_confirm_step = step
                        break

            if last_non_confirm_step:
                reviewed_type = last_non_confirm_step.step_type.value
                inputs["step_to_review_id"] = last_non_confirm_step.step_id
                inputs["step_to_review_type"] = reviewed_type

                # A CONFIRM gating an ADJUDICATE ruling must review the proposed
                # patch against the PRE-ruling effective task/plan — not against
                # the ruling's own not-yet-approved rewrite. The generic overrides
                # above already installed the (tail) adjudicated_description/plan
                # as the effective text; recompute the baseline EXCLUDING this
                # unapproved ruling so the reviewer's comparison anchor hasn't
                # already moved before the gate clears. The proposed rewrite still
                # reaches the reviewer via the reviewed step's own outputs.
                if last_non_confirm_step.step_type == StepType.ADJUDICATE:
                    adj_id = last_non_confirm_step.step_id
                    inputs["task_description"] = _compose_effective_task_description(
                        flow, exclude_step_id=adj_id
                    )

                # Single YAML read for the entire CONFIRM resolution —
                # consolidates what used to be three separate config
                # reads (load_confirmation_config + load_agent_registry
                # + load_agents) per step transition.
                resolved = resolve_confirm_inputs(
                    self.project_root, reviewed_type,
                )

                if resolved is None:
                    # A flow created while this step's CONFIRM was still
                    # always-on carries it in its persisted sequence with no
                    # config entry behind it. Resolve it the way the retired
                    # rule did (unattended LLM review) rather than through the
                    # human fallback below — retiring a gate must not turn an
                    # in-flight automated review into a blocking human one.
                    # The pre-model marker is required so this stays confined to
                    # those flows: on a flow created after the degrade the same
                    # unresolved CONFIRM is config drift (the entry was deleted
                    # mid-flow), and drift must reach the warning + human
                    # fallback below instead of silently buying an LLM review.
                    resolved = resolve_retired_always_on_confirm_inputs(
                        self.project_root,
                        reviewed_type,
                        flow_predates_degrade=(
                            PLAN_DECOMPOSITION_KEY not in flow.state.context
                        ),
                    )

                if resolved is None:
                    # Defensive fallback — insert_confirmation_steps is
                    # the only path that puts CONFIRM into the sequence
                    # and it gates on this same dict, so reaching here
                    # implies config drift between dict snapshots (e.g.
                    # YAML edited mid-flow). Behave as 'human' so the
                    # user can decide what to do. max_iterations is
                    # carried as None for schema uniformity across
                    # branches even though confirm_handler ignores it
                    # on the human path.
                    logger.warning(
                        "CONFIRM step inserted for %s but no entry under "
                        "confirmation.steps; defaulting to human reviewer",
                        reviewed_type,
                    )
                    inputs["reviewer"] = "human"
                    inputs["max_iterations"] = None
                else:
                    # Use explicit 'is not None' so a persisted 0 from
                    # an older engine build surfaces loudly instead of
                    # being silently replaced by the default. The
                    # current parser rejects 0/negative → None, so the
                    # 'or 3' form works in practice, but a strict check
                    # keeps resume-path behavior predictable across
                    # schema revisions.
                    raw_iters = resolved.get("max_iterations")
                    max_iters = raw_iters if raw_iters is not None else 3

                    reviewer = resolved.get("reviewer")
                    if reviewer == "human":
                        inputs["reviewer"] = "human"
                        # Carry max_iterations uniformly so callers
                        # that read step.inputs['max_iterations']
                        # unconditionally don't trip on a KeyError.
                        # confirm_handler ignores it on this path.
                        inputs["max_iterations"] = max_iters
                    elif reviewer is None:
                        inputs["reviewer"] = None
                        inputs["agents"] = resolved["agents"]
                        inputs["max_iterations"] = max_iters
                    else:
                        # Agent-name reference. resolve_confirm_inputs
                        # already performed the registry lookup and
                        # either returned the resolved agent list or
                        # raised ValueError. Guard against 'agents'
                        # being unexpectedly absent — this would only
                        # happen if the helper contract breaks and is
                        # treated as a StateMachineError.
                        agents = resolved.get("agents")
                        if not agents:
                            raise StateMachineError(
                                f"confirmation.steps.{reviewed_type}.reviewer "
                                f"references agent {reviewer!r} but no "
                                f"agent dict was resolved; this is an "
                                f"internal invariant violation"
                            )
                        inputs["reviewer"] = reviewer
                        inputs["agents"] = agents
                        inputs["max_iterations"] = max_iters

        # Special handling for TEST step when in fix iteration
        if step_type == StepType.TEST:
            # Inject the frozen pre-implement baseline so the test step can
            # classify failures as inherited (in baseline) vs introduced.
            # Not-yet-captured (None) injects as [] so the consumer treats
            # every failure as introduced (the safe default).
            inputs["baseline_failures"] = list(flow.state.baseline_failures or [])
            fix_iteration = flow.state.get_fix_iteration()
            if fix_iteration > 0:
                inputs["is_fix_iteration"] = True
                inputs["fix_iteration"] = fix_iteration

        # Special handling for SELF_CHECK step: ensure it receives test_results and changes_made
        if step_type == StepType.SELF_CHECK:
            workflow_cfg = self._get_workflow_config()
            passes_required = self._get_self_check_passes_required()
            inputs["self_check_passes_required"] = passes_required
            self._prepare_self_check_scope(flow, inputs, passes_required)
            pass_index = int(inputs.get("self_check_pass_index", 1) or 1)
            # Serialized for old consumers, but permanently false: repeated or
            # converged findings are never a completed SELF_CHECK outcome.
            inputs["self_check_convergence_enabled"] = False
            inputs["self_check_defer_fix_threshold"] = workflow_cfg.self_check_defer_fix_threshold
            # Periodic adjudication still consumes the same persisted setting;
            # it is evaluated after a finding-bearing REVISION_NEEDED round.
            inputs["adjudicate_period"] = workflow_cfg.adjudicate_period

            # Defer-fix stash (item 1) lifecycle. pass #1 is the start of every
            # fix-loop round (and the very first round), so it resets the
            # accumulated stash; pass #2+ inherits the stash the prior pass
            # wrote back into context. The stash lives on ``flow.state.context``
            # so it persists with engine.json across ``--resume``.
            if pass_index == 1:
                flow.state.context["self_check_deferred_issues"] = []
            inputs["self_check_deferred_issues"] = copy.deepcopy(
                flow.state.context.get("self_check_deferred_issues", [])
            )

            # Always populate max_fix_iterations so the handler never has to
            # re-load WorkflowConfig on the initial pass (the per-transition
            # cache covers the second call within the same transition, but
            # the handler runs in a different code path and would re-parse
            # tianluo.yaml otherwise). Treat the input as a hard contract: the
            # handler may now assume it is always present and an int.
            inputs["max_fix_iterations"] = self._get_max_fix_iterations()
            fix_iteration = flow.state.get_fix_iteration()
            if fix_iteration > 0:
                inputs["fix_iteration"] = fix_iteration
                inputs["fix_history"] = copy.deepcopy(flow.state.fix_history)

            # Pass through the un-decorated task_description base (refined
            # or canonical, NO interjection section) and the structured
            # user_interjections list so ``_build_source_pool`` can build
            # a verbatim-quote source pool from clean inputs. Without these
            # the source pool would only have the COMPOSED task_description
            # — which contains our own ``## Additional Instructions`` boiler-
            # plate header that an LLM could quote verbatim to slip an
            # ungrounded issue past validation.
            inputs["task_description_base"] = _effective_task_description_base(flow)
            inputs["user_interjections"] = copy.deepcopy(
                flow.state.context.get("user_interjections", [])
            )

            # Preserve the explicit field for old history/resume consumers. The
            # effective base above is already the adjudicated description, and
            # modern source-pool validation never consults adjudicated_plan or
            # task_groups.
            adjudicated_desc = _latest_adjudicated_output(flow, "adjudicated_description")
            if isinstance(adjudicated_desc, str) and adjudicated_desc:
                inputs["adjudicated_description"] = adjudicated_desc

            # Inject prev_self_check_issues whenever this is the first pass
            # of a fix-loop round (pass_index == 1 AND fix_iteration > 0).
            # Previous issues are injected regardless of the deprecated
            # convergence setting: with the new schema (verbatim_quote +
            # evidence_lines + previous_issue_resolutions), the LLM is required to
            # explicitly declare each prev_issue as fixed/still_present, so
            # passing prev_issues is essential for correct review.
            # Pass 2+ deliberately omits prev_issues to provide an
            # independent fresh review (N-pass invariant unchanged).
            if pass_index == 1 and fix_iteration > 0:
                for step_id in reversed(flow.state.step_history):
                    step = flow.state.steps.get(step_id)
                    if not step or step.status == StepStatus.FAILED:
                        continue
                    if (step.step_type == StepType.SELF_CHECK
                            and step.status == StepStatus.REVISION_NEEDED):
                        inputs["prev_self_check_issues"] = copy.deepcopy(
                            step.outputs.get("issues", [])
                        )
                        break

            # Ensure test_results and changes_made are present (from history loop above)
            # If not already set, find them from step history
            for step_id in reversed(flow.state.step_history):
                step = flow.state.steps.get(step_id)
                if not step or step.status == StepStatus.FAILED:
                    continue
                if step.step_type == StepType.TEST and "test_results" not in inputs:
                    inputs["test_results"] = step.outputs.get("test_results")
                elif step.step_type == StepType.IMPLEMENT and "changes_made" not in inputs:
                    inputs["changes_made"] = {
                        "files_changed": step.outputs.get("files_changed", []),
                        "implemented_groups": step.outputs.get("implemented_groups", []),
                    }

        # Special handling for the charter-refactor steps INVARIANT_CHECK and
        # CHARTER_FRESHNESS. Both anchor on the charter, which is frozen once at
        # flow start (see _freeze_invariant_anchors) so a charter edited mid-flow
        # — e.g. one CHARTER_FRESHNESS flagged — cannot retroactively change what
        # the invariant check anchors against. ``changes_made`` and
        # ``task_description`` are already populated above (from the IMPLEMENT
        # history loop and the effective-task composition respectively); the
        # diff-dependent why-comments are harvested by the handler at check time.
        if step_type in (StepType.INVARIANT_CHECK, StepType.CHARTER_FRESHNESS):
            anchors = flow.state.context.get("invariant_anchors") or {}
            charter_text = anchors.get("charter")
            if isinstance(charter_text, str) and charter_text:
                inputs["charter"] = charter_text
            if step_type == StepType.INVARIANT_CHECK:
                # INVARIANT_CHECK joins the shared fix loop (TEST / SELF_CHECK),
                # so it must see the same global bound and current iteration the
                # state machine enforces centrally.
                inputs["max_fix_iterations"] = self._get_max_fix_iterations()
                fix_iteration = flow.state.get_fix_iteration()
                if fix_iteration > 0:
                    inputs["fix_iteration"] = fix_iteration

        # Special handling for IMPLEMENT step when in fix iteration
        if step_type == StepType.IMPLEMENT:
            # Check if we're in a fix loop
            fix_iteration = flow.state.get_fix_iteration()
            if fix_iteration > 0:
                # Include fix context for the implement step
                inputs["fix_iteration"] = fix_iteration
                inputs["fix_history"] = copy.deepcopy(flow.state.fix_history)

                # Find the most recent TEST step for fix context. (The retired
                # VERIFY_SPEC step no longer contributes verification_result /
                # fix_context; INVARIANT_CHECK routes its own fix instructions
                # through the shared fix loop.)
                for step_id in reversed(flow.state.step_history):
                    step = flow.state.steps.get(step_id)
                    if not step or step.status == StepStatus.FAILED:
                        continue
                    if step.step_type == StepType.TEST:
                        # Always overwrite with a deep copy in the fix loop —
                        # the base-loop version is a direct reference, which would
                        # leak later mutations to step.outputs.
                        inputs["test_results"] = copy.deepcopy(
                            step.outputs.get("test_results")
                        )
                        break

        # --- Root-cause investigation context -------------------------------
        # INVARIANT: the investigation report travels ONLY through these
        # dedicated inputs keys. It must never be merged into
        # ``task_description`` / ``task_description_base`` / any adjudicated
        # description — self_check builds its verbatim-quote source pool from
        # that intent chain (``self_check._build_source_pool``), so report text
        # in the chain would let an LLM cite its own speculative hypothesis as
        # "the user asked for this" and slip an ungrounded issue past evidence
        # validation. The report is a lead for the planner, not a statement of
        # intent.
        # SUMMARIZE is in this set for a different reason than PLAN/IMPLEMENT:
        # it does not aim work with the report, it *is* where the report reaches
        # the user. The survey sequence is ANALYZE -> INVESTIGATE -> SUMMARIZE,
        # so without this the only artifact a survey flow writes to disk would
        # describe a session that changed nothing and ran no tests, while the
        # answer the user asked for stayed buried in engine.json's step outputs.
        if step_type in (
            StepType.PLAN, StepType.IMPLEMENT, StepType.SUMMARIZE,
        ) and investigation_reports:
            inputs["root_cause_report"] = copy.deepcopy(investigation_reports[-1])
            inputs["investigation_history"] = copy.deepcopy(investigation_reports)
            # Rounds ran out before a conclusive cause: the report is the best
            # available hypothesis and downstream prompts must say so.
            if flow.state.context.get("investigation_exhausted"):
                inputs["investigation_exhausted"] = True

        if step_type == StepType.INVESTIGATE:
            # Round N sees rounds 1..N-1 so it can push further instead of
            # repeating experiments that already came back inconclusive.
            inputs["investigation_iteration"] = (
                self._count_consecutive_investigate_completed(flow) + 1
            )
            inputs["investigation_max_iterations"] = (
                self._get_investigation_max_iterations()
            )
            inputs["previous_investigation_reports"] = copy.deepcopy(
                investigation_reports
            )

        return inputs

    def _write_flow_meta(self, flow: FlowInstance) -> None:
        """Write _meta.json to the flow's history directory.

        Records SE3 version, Python version, and creation timestamp
        for post-hoc debugging. Skips if file already exists (resume scenario).

        Args:
            flow: Current flow instance
        """
        history_dir = _history_dir(self.project_root, flow.flow_id)
        meta_path = history_dir / "_meta.json"

        if meta_path.exists():
            logger.debug(f"_meta.json already exists for flow {flow.flow_id}, skipping")
            return

        history_dir.mkdir(parents=True, exist_ok=True)

        meta = {
            "se3_version": se3_version,
            "python_version": sys.version,
            "created_at": datetime.now().isoformat(),
        }

        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
            logger.debug(f"Wrote _meta.json for flow {flow.flow_id}")
        except OSError as e:
            logger.warning(f"Failed to write _meta.json: {e}")

    def _record_baseline_commit(self, flow: FlowInstance) -> None:
        """Record the current HEAD commit hash as the baseline for change detection.

        The baseline commit is used by the commit step to detect changes via
        ``git diff <baseline> HEAD`` instead of relying on ``git status --porcelain``,
        which fails in multi-worktree scenarios where changes are merged and the
        working tree is clean.

        Does nothing if a baseline commit is already recorded (e.g., on flow resume).

        Args:
            flow: Current flow instance
        """
        if flow.baseline_commit:
            logger.debug(f"Baseline commit already set: {flow.baseline_commit[:8]}")
            return

        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=self.project_root,
            )
            if result.returncode == 0 and result.stdout.strip():
                flow.baseline_commit = result.stdout.strip()
                self.persistence.save_flow(flow)
                logger.info(f"Recorded baseline commit: {flow.baseline_commit[:8]}")
            else:
                logger.warning("Failed to get HEAD commit hash for baseline")
        except Exception as e:
            logger.warning(f"Failed to record baseline commit: {e}")

    def init_flow(self, flow: FlowInstance) -> None:
        """Initialize a flow after creation or before resumption.

        Part of the flow lifecycle API: create_flow() → init_flow() → run_step()/transition_to_next().

        Writes ``_meta.json`` (version info) and records the baseline commit for
        change detection.  Both operations are idempotent, so calling this on a
        resumed flow is safe (existing meta / baseline are preserved).

        Args:
            flow: Flow instance to initialize
        """
        # Fail-fast: validate workflow configuration on every start/resume
        WorkflowConfig.load(self.project_root)
        # Same for the investigation cap. The state machine is its only reader,
        # and it deliberately degrades to defaults mid-transition (an unusable
        # cap must not abort a running flow), so without this check a typo like
        # `max_iterations: -1` would never be reported to anyone.
        from ..config import InvestigationConfig

        InvestigationConfig.load(self.project_root)

        self._write_flow_meta(flow)
        self._record_baseline_commit(flow)
        self._start_baseline_capture(flow)
        self._freeze_invariant_anchors(flow)

    def _freeze_invariant_anchors(self, flow: FlowInstance) -> None:
        """Freeze the INVARIANT_CHECK anchor set once per flow, at flow start.

        The anchored invariant check (which replaced the retired spec_gate /
        verify_spec) guards only invariants that were *explicitly recorded*: the
        task_description, the charter full text, and the why-comments of the
        touched code. The first two are stable project-/flow-level text, so they
        are frozen here into ``flow.state.context['invariant_anchors']`` and the
        frozen charter is injected into every INVARIANT_CHECK / CHARTER_FRESHNESS
        step — so a charter edited mid-flow (e.g. one a CHARTER_FRESHNESS advisory
        prompted) cannot retroactively change what the check anchors against. The
        why-comments are diff-dependent (the touched set is unknown until after
        implement), so the handler harvests them at check time — but reads each
        touched file's content at the **frozen baseline commit** (recorded here
        by ``_record_baseline_commit``) as well as the working tree, so the
        original why-comment anchor text is preserved even when the diff deleted
        or rewrote a comment that documented a binding invariant.

        Captured a **single time** per flow and never overwritten (idempotent
        across ``--resume`` and fix-loop re-entries). The one-shot guard keys off
        the context key's presence, so a legitimately empty charter is still
        recorded once and not re-read.

        Never raises: a freeze failure must not crash the flow (the handler falls
        back to an on-disk charter read when no frozen anchor is present).
        """
        if "invariant_anchors" in flow.state.context:
            return
        try:
            from .charter import load_charter

            charter_text = load_charter(self.project_root)
            flow.state.context["invariant_anchors"] = {
                "charter": charter_text or "",
                "task_description": flow.task_description or "",
            }
            self.persistence.save_flow(flow)
            logger.info(
                "invariant_check: froze anchor set at flow start (charter %d chars)",
                len(charter_text or ""),
            )
        except Exception as e:  # noqa: BLE001 — never crash the flow on anchor setup
            logger.warning("Failed to freeze invariant anchors: %s", e)

    def _start_baseline_capture(self, flow: FlowInstance) -> None:
        """Launch (or reuse a cached) pre-implement test baseline at flow start.

        Runs at flow creation/init — concurrently with the LLM-bound
        ``analyze → plan → confirm`` steps, which do not write source — so the
        ~3-minute suite run is hidden under that pre-implement work and adds
        ~0 wall-clock to a typical flow.

        Three paths:

        - **Already measured** (``state.baseline_failures`` is not ``None``):
          a resumed flow that captured its baseline in an earlier session.
          Nothing to launch.
        - **Cache hit** on the ``HEAD sha + dirty hash`` key: reuse the measured
          set immediately and persist it onto the flow — no background run.
        - **Cache miss**: launch :class:`BaselineCapture` in the background and
          stash its handle for :meth:`_ensure_baseline_ready` to await.

        Never raises: a baseline-capture failure must not crash the flow. The
        synchronous fallback in :meth:`_ensure_baseline_ready` covers the case
        where launch silently failed.
        """
        # Idempotent: a resumed flow that already has a measured baseline (even
        # the empty-set case, distinguished from the ``None`` "not measured"
        # sentinel) must not re-launch.
        if flow.state.baseline_failures is not None:
            logger.debug(
                "Baseline already measured for flow %s (%d failures); skipping capture launch",
                flow.flow_id,
                len(flow.state.baseline_failures),
            )
            return

        try:
            from .test_baseline import (
                BaselineCapture,
                compute_baseline_key,
                load_cached,
            )

            key = compute_baseline_key(self.project_root)
            self._baseline_key = key

            cached = load_cached(self.project_root, key)
            if cached is not None:
                flow.state.baseline_failures = sorted(cached)
                self.persistence.save_flow(flow)
                logger.info(
                    "Baseline cache hit (key=%s, %d failures); skipping background capture",
                    key,
                    len(cached),
                )
                return

            # Cache miss → launch the suite in the background, overlapping the
            # analyze/plan/confirm steps that follow.
            self._baseline_capture = BaselineCapture(self.project_root).launch()
            logger.info("Baseline capture launched in background (key=%s)", key)
        except Exception as e:  # noqa: BLE001 — never crash the flow on capture setup
            logger.warning("Failed to start baseline capture: %s", e)

    def _settle_baseline_before_investigation(self, flow: FlowInstance) -> None:
        """Resolve an in-flight baseline suite BEFORE an INVESTIGATE step runs.

        INVARIANT: no step that may write the working tree runs while the
        background baseline suite launched by :meth:`_start_baseline_capture`
        is still in flight.

        WHY: that capture overlaps the ``analyze → plan → confirm`` window, and
        its correctness rests on every step in that window being read-only.
        INVESTIGATE breaks that premise — it is explicitly allowed to add
        temporary logging, probe patches and scratch scripts to the live tree
        (net-zero diff is enforced only at the step's *end*), while the
        background pytest runs with ``cwd=project_root`` against that same
        tree. Left unserialized, the suite would import the probes and record
        their fallout as "pre-existing" failures — poisoning both
        ``state.baseline_failures`` and the on-disk cache entry keyed on the
        *pre*-investigation clean tree. Downstream, TEST subtracts that set as
        inherited, so a real regression in exactly those tests is waved through
        (and symmetrically, a probe that masks a pre-existing failure gets it
        blamed on IMPLEMENT).

        Which way to serialize depends on whether this flow ever consumes a
        baseline, i.e. whether a TEST step remains in its sequence:

        - **TEST ahead** (a bugfix with an inserted investigation): await the
          run through the ordinary :meth:`_ensure_baseline_ready` path. It was
          launched against the clean flow-start tree and finishes before any
          probe lands, so the measurement stays valid and cacheable — and the
          pre-IMPLEMENT wait later becomes free.
        - **No TEST** (survey: ANALYZE → INVESTIGATE → SUMMARIZE): the flow
          never reads a baseline, so blocking on a multi-minute suite would be
          pure latency. Kill and discard it — nothing is measured, so nothing
          poisoned can reach the cache either.

        No-op when nothing is in flight (cache hit, resumed flow with a
        measured baseline, or an already-reaped handle), so a repeat
        investigation round costs nothing.
        """
        if self._baseline_capture is None:
            return

        needs_baseline = StepType.TEST in (flow.state.selected_steps or [])
        if needs_baseline:
            logger.info(
                "Awaiting the background baseline suite before investigate: the "
                "investigation may write probes into the tree it is running against"
            )
            self._ensure_baseline_ready(flow)
            return

        logger.info(
            "Discarding the background baseline suite before investigate: this "
            "flow has no TEST step, so no baseline is ever consumed"
        )
        self.cleanup_baseline_capture()

    def _ensure_investigation_baseline(
        self, flow: FlowInstance, step: Step
    ) -> None:
        """Freeze an INVESTIGATE step's net-zero-diff baseline into its inputs.

        Runs just before the step is marked RUNNING and persisted, so the
        baseline reaches disk before the investigation call — the only window
        in the step long enough for a hard kill to matter. Idempotent, so a
        Retry or a ``--resume`` re-entry keeps the first attempt's baseline and
        the leftovers it was taken to expose stay visible.

        Never fatal: an unavailable snapshot degrades the guard to *undecidable*
        downstream, which must not stop the investigation from running.
        """
        try:
            from .steps._project_root import resolve_flow_project_root
            from .steps.investigate import ensure_workspace_baseline

            ensure_workspace_baseline(step, resolve_flow_project_root(flow))
        except Exception:
            logger.debug(
                "Failed to capture the investigation workspace baseline",
                exc_info=True,
            )

    def _review_scope_manager(self, flow: FlowInstance) -> ReviewScopeManager:
        """Return the runtime review-scope store for this flow checkout."""
        from .steps._project_root import resolve_flow_project_root

        return ReviewScopeManager(resolve_flow_project_root(flow), flow.flow_id)

    @staticmethod
    def _review_scope_context(flow: FlowInstance) -> Dict[str, Any]:
        value = flow.state.context.get("review_scope")
        if not isinstance(value, dict):
            value = {}
            flow.state.context["review_scope"] = value
        return value

    def _self_check_round_controller(
        self, flow: FlowInstance
    ) -> SelfCheckRoundController:
        return SelfCheckRoundController(flow.state.context)

    @staticmethod
    def _review_baseline_from(value: Any) -> Optional[ReviewBaseline]:
        return ReviewBaseline.from_dict(value)

    def _fix_baselines(
        self, flow: FlowInstance, scope_context: Dict[str, Any]
    ) -> Dict[str, ReviewBaseline]:
        """Load every captured fix baseline by id from the runtime store."""
        history = scope_context.get("fix_baseline_history")
        manager = self._review_scope_manager(flow)
        result: Dict[str, ReviewBaseline] = {}
        if not isinstance(history, list):
            return result
        for entry in history:
            if not isinstance(entry, dict):
                continue
            baseline_id = str(entry.get("baseline_id") or "")
            if not baseline_id:
                continue
            baseline = manager.load_baseline(baseline_id)
            if baseline is not None:
                result[baseline_id] = baseline
        return result

    def _incremental_fix_baseline(
        self, flow: FlowInstance, scope_context: Dict[str, Any]
    ) -> Optional[ReviewBaseline]:
        """The EARLIEST fix baseline not yet covered by a review round.

        Multiple FIXes can run with no SELF_CHECK round between them. A round
        diffed from the earliest uncovered baseline spans the union of every
        such fix's changes, so a defect introduced by an earlier unreviewed
        fix keeps its causal anchors inside the scope — diffing from only the
        LAST fix would drop that fix's delta from the round entirely. When
        the earliest uncovered baseline cannot be loaded the union cannot be
        reconstructed, so this returns None and the round degrades to the
        full fallback instead of silently narrowing the scope.
        """
        history = scope_context.get("fix_baseline_history")
        covered = scope_context.get("covered_fix_baseline")
        latest_dict = scope_context.get("latest_fix_baseline")
        latest_id = (
            latest_dict.get("baseline_id")
            if isinstance(latest_dict, dict)
            else None
        )
        if not isinstance(history, list) or not history:
            # Pre-history persisted flows and synthetic callers that carry
            # only the latest-fix-baseline key: there the latest baseline IS
            # the earliest unreviewed one.
            return self._review_baseline_from(latest_dict)
        fix_baselines = self._fix_baselines(flow, scope_context)
        found_covered = not covered
        for entry in history:
            if not isinstance(entry, dict):
                continue
            baseline_id = str(entry.get("baseline_id") or "")
            if not baseline_id:
                continue
            if not found_covered:
                if baseline_id == covered:
                    found_covered = True
                continue
            # The first entry AFTER the covered baseline is the earliest
            # unreviewed fix.
            if baseline_id in fix_baselines:
                return fix_baselines[baseline_id]
            return None
        # Everything in history is covered: only a newer fix baseline beyond
        # the covered marker is still unreviewed (and its history entry may
        # be missing in pre-history synthetic callers).
        if latest_id and latest_id != covered:
            baseline = fix_baselines.get(latest_id)
            if baseline is not None:
                return baseline
            return self._review_baseline_from(latest_dict)
        return None

    def _ensure_implementation_review_baseline(
        self, flow: FlowInstance, step: Step
    ) -> None:
        """Persist the pre-implementation code baseline exactly once."""
        scope_context = self._review_scope_context(flow)
        if "implementation_baseline" in scope_context:
            return

        manager = self._review_scope_manager(flow)
        already_started = bool(
            flow.state.get_fix_iteration() > 0
            or step.inputs.get("is_fix_iteration")
            or step.started_at is not None
            or step.outputs.get("files_changed")
            or step.outputs.get("changes_made")
        )
        try:
            if already_started:
                baseline = manager.unavailable_baseline(
                    "implementation",
                    "legacy/resumed flow crossed the implementation baseline "
                    "boundary before diff-scoped review was available",
                )
            else:
                baseline = manager.capture("implementation")
        except Exception as exc:  # noqa: BLE001 - persist safe degradation
            logger.warning("Failed to persist implementation review baseline: %s", exc)
            baseline = ReviewBaseline(
                baseline_id=f"implementation-{uuid.uuid4().hex[:12]}",
                kind="implementation",
                flow_id=flow.flow_id,
                captured_at=datetime.now().isoformat(),
                project_root=str(self.project_root),
                available=False,
                diagnostics=[str(exc)],
            )
        scope_context["implementation_baseline"] = baseline.to_dict()
        # Persist immediately. The next operation is the writable IMPLEMENT
        # handler, so relying only on the later generic RUNNING save would leave
        # a crash window in which modifications exist but their baseline does not.
        self.persistence.save_flow(flow)

    def _capture_fix_review_baseline(
        self, flow: FlowInstance, iteration: int
    ) -> None:
        """Persist an independent baseline immediately before one FIX call."""
        scope_context = self._review_scope_context(flow)
        manager = self._review_scope_manager(flow)
        try:
            baseline = manager.capture(f"fix-{iteration}")
        except Exception as exc:  # noqa: BLE001 - full fallback remains safe
            logger.warning("Failed to persist fix review baseline: %s", exc)
            baseline = ReviewBaseline(
                baseline_id=f"fix-{iteration}-{uuid.uuid4().hex[:12]}",
                kind=f"fix-{iteration}",
                flow_id=flow.flow_id,
                captured_at=datetime.now().isoformat(),
                project_root=str(self.project_root),
                available=False,
                diagnostics=[str(exc)],
            )
        scope_context["latest_fix_baseline"] = baseline.to_dict()
        history = scope_context.setdefault("fix_baseline_history", [])
        if not isinstance(history, list):
            history = []
            scope_context["fix_baseline_history"] = history
        history.append({
            "fix_iteration": int(iteration),
            "baseline_id": baseline.baseline_id,
            "available": baseline.available,
            "diagnostics": list(baseline.diagnostics),
        })

    def _prepare_self_check_scope(
        self,
        flow: FlowInstance,
        inputs: Dict[str, Any],
        passes_required: int,
    ) -> None:
        """Attach one persisted review round and its reconstructed diff."""
        scope_context = self._review_scope_context(flow)
        had_round_state = isinstance(
            flow.state.context.get("self_check_review"), dict
        )
        implementation_baseline = self._review_baseline_from(
            scope_context.get("implementation_baseline")
        )
        # WHY the earliest uncovered fix baseline instead of the latest one:
        # when two or more FIXes run with no SELF_CHECK round between them,
        # the latest baseline alone scopes only the last fix's delta — a
        # defect introduced by an earlier unreviewed fix would have no causal
        # anchor in the round and its evidence would be dropped as bad.
        incremental_fix_baseline = self._incremental_fix_baseline(
            flow, scope_context
        )
        effective_description = _compose_effective_task_description(flow)
        controller = self._self_check_round_controller(flow)
        before_active = controller.active_round
        active = controller.prepare_round(
            requirement_text=effective_description,
            fix_iteration=flow.state.get_fix_iteration(),
            passes_required=passes_required,
            implementation_baseline=implementation_baseline,
            latest_fix_baseline=incremental_fix_baseline,
        )
        if controller.active_round is not before_active:
            # A NEW round was created: every fix baseline up to the latest one
            # is now inside its scope (a full round diffs from the
            # implementation baseline, an incremental one from the earliest
            # uncovered fix baseline — both span the latest fix).
            latest = scope_context.get("latest_fix_baseline")
            if isinstance(latest, dict) and latest.get("baseline_id"):
                scope_context["covered_fix_baseline"] = latest.get("baseline_id")
        if not had_round_state:
            # Old persisted flows (and pre-scope synthetic callers) may already
            # be between pass #1 and pass #N. Adopt that position once, then
            # let the explicit persisted controller own all later passes.
            # WHY the step's own recorded index wins: when a PENDING step is
            # refreshed on resume, that step already sits at the tail of
            # step_history and ends the completed-pass streak, so the tail
            # count would answer "1" and re-run the whole chain. Freshly built
            # inputs carry no index, and there the completed tail is the only
            # evidence of where a pre-upgrade chain stood.
            persisted_index = inputs.get("self_check_pass_index")
            if (
                isinstance(persisted_index, int)
                and not isinstance(persisted_index, bool)
                and persisted_index >= 1
            ):
                adopted = persisted_index
            else:
                adopted = self._count_consecutive_self_check_completed(flow) + 1
            active["pass_index"] = min(
                int(active.get("passes_required", passes_required) or passes_required),
                adopted,
            )

        fix_baselines = self._fix_baselines(flow, scope_context)
        if active.get("baseline_kind") == "implementation":
            baseline = implementation_baseline
        elif (
            active.get("baseline_id")
            and active.get("baseline_id") in fix_baselines
        ):
            baseline = fix_baselines[active.get("baseline_id")]
        elif (
            incremental_fix_baseline is not None
            and incremental_fix_baseline.baseline_id == active.get("baseline_id")
        ):
            baseline = incremental_fix_baseline
        elif (
            implementation_baseline is not None
            and implementation_baseline.baseline_id == active.get("baseline_id")
        ):
            baseline = implementation_baseline
        else:
            baseline = None

        scope = self._review_scope_manager(flow).resolve(
            str(active.get("scope_mode", "full")),
            baseline,
            full_baseline=implementation_baseline,
            # The implement step's self-reported files are consulted for one
            # case only: a path git ignores is invisible to baseline capture,
            # so without them a real flow change would never be diffed,
            # anchored or reviewed (see ReviewScopeManager.reconstruct).
            declared_paths=_declared_changed_paths(inputs),
        )
        if scope.scope_mode != active.get("scope_mode"):
            active["scope_mode"] = scope.scope_mode
            active["baseline_id"] = scope.baseline_id
            active["baseline_kind"] = "implementation"
            active["round_reason"] = "incremental_undecidable_full_fallback"
            if int(active.get("pass_index", 1) or 1) <= 1:
                # The degrade happened before any pass ran, so the whole round
                # genuinely reviews the full implementation diff and is
                # credited as a full round. A degrade on a LATER pass leaves
                # the accounting mode alone: pass #1 already reviewed only the
                # fix delta, so the mandatory full closure round is still owed.
                active["round_scope_mode"] = scope.scope_mode

        active["scope_changed_paths"] = list(scope.changed_paths)
        active["scope_diff_artifact"] = scope.artifact_path
        active["scope_undecidable"] = scope.undecidable
        active["scope_diagnostic"] = scope.diagnostic
        active["scope_causal_anchors"] = copy.deepcopy(scope.causal_anchors)
        active["scope_deletion_anchors"] = copy.deepcopy(scope.deletion_anchors)
        active["scope_task_changed_paths"] = list(scope.task_changed_paths)
        active["scope_task_causal_anchors"] = copy.deepcopy(
            scope.task_causal_anchors
        )
        active["scope_task_deletion_anchors"] = copy.deepcopy(
            scope.task_deletion_anchors
        )
        active["scope_task_available"] = scope.task_scope_available
        active["scope_task_diagnostic"] = scope.task_scope_diagnostic

        inputs.update({
            "self_check_round_id": active.get("round_id", ""),
            "self_check_pass_index": int(active.get("pass_index", 1) or 1),
            "self_check_passes_required": int(
                active.get("passes_required", passes_required) or passes_required
            ),
            "scope_mode": scope.scope_mode,
            "requested_scope_mode": scope.requested_mode,
            "baseline_id": scope.baseline_id,
            "scope_changed_paths": list(scope.changed_paths),
            "scope_causal_anchors": copy.deepcopy(scope.causal_anchors),
            "scope_deletion_anchors": copy.deepcopy(scope.deletion_anchors),
            # The whole-task evidence domain of an incremental round. Empty on a
            # full round, where the round baseline already spans the whole task.
            "scope_task_baseline_id": scope.task_baseline_id,
            "scope_task_changed_paths": list(scope.task_changed_paths),
            "scope_task_causal_anchors": copy.deepcopy(scope.task_causal_anchors),
            "scope_task_deletion_anchors": copy.deepcopy(
                scope.task_deletion_anchors
            ),
            "scope_task_diff_artifact": scope.task_artifact_path,
            "scope_task_available": scope.task_scope_available,
            "scope_task_diagnostic": scope.task_scope_diagnostic,
            "scope_diff": scope.unified_diff,
            "scope_diff_artifact": scope.artifact_path,
            "scope_undecidable": scope.undecidable,
            "scope_diagnostic": scope.diagnostic,
            "scope_fallback_from_incremental": scope.fallback_from_incremental,
            "requirement_fingerprint": active.get("requirement_fingerprint", ""),
            "self_check_round_reason": active.get("round_reason", ""),
        })

    def _refresh_self_check_scope(self, flow: FlowInstance, step: Step) -> None:
        """Refresh a pending SELF_CHECK when its requirement authority changed."""
        passes_required = int(
            step.inputs.get("self_check_passes_required")
            or self._get_self_check_passes_required()
        )
        controller = self._self_check_round_controller(flow)
        effective_description = _compose_effective_task_description(flow)
        if controller.requirements_changed(effective_description):
            controller.force_full("effective_requirements_changed")

        # WHY: verbatim_quote validation resolves against the source pool built
        # from these two inputs, which were snapshotted when the step was first
        # constructed. An interjection recorded afterwards (Ctrl-C on a running
        # SELF_CHECK, or a web interjection drained onto a pending one) rewrites
        # only ``task_description``; without refreshing the clean base and the
        # structured interjection list here, a finding quoting the new
        # instruction is dropped as quote-not-in-source and the requirement
        # becomes permanently unenforceable by SELF_CHECK.
        step.inputs["task_description"] = effective_description
        step.inputs["task_description_base"] = _effective_task_description_base(flow)
        step.inputs["user_interjections"] = copy.deepcopy(
            flow.state.context.get("user_interjections", [])
        )

        self._prepare_self_check_scope(flow, step.inputs, passes_required)

    def _ensure_baseline_ready(self, flow: FlowInstance) -> None:
        """Block until the pre-implement baseline is measured, before IMPLEMENT.

        Called right before the ``implement`` step's first write so the
        introduced-vs-inherited classification has a frozen reference taken
        *before* this flow modified anything (acceptance criterion: an
        introduced failure can never be laundered into the baseline).

        Idempotent: when ``state.baseline_failures`` is already set (cache hit,
        a prior pass, or a fix-loop re-entry into IMPLEMENT) it returns at once
        without re-measuring.

        Every wait is **time-bounded** (see :func:`resolve_baseline_timeout`):
        a hung test (deadlock, infinite loop, stuck IO) is killed at the bound
        and treated as a failed measurement, so the baseline run can never
        reintroduce the unbounded-runtime failure mode this change exists to
        eliminate — just relocated to flow start.

        Resolution order:

        1. **Background handle** from :meth:`_start_baseline_capture`: await it
           up to the time bound, killing the subprocess on expiry.
        2. **Synchronous fallback**: if there is no handle, or the background
           run returned the ``None`` failure sentinel for an *infra* reason
           (could not produce parseable results), run one authoritative,
           time-bounded measurement here. A background *timeout* skips this step
           (re-running a hung suite synchronously would just hang again).
        3. **Empty-baseline last resort**: if even the synchronous run fails or
           times out, fall back to an empty baseline with a loud warning. This
           errs toward treating current failures as *introduced* (never launders
           a real regression into the baseline), at the cost of possibly
           re-flagging a genuinely pre-existing failure — the safe direction.

        The resolved set is persisted onto the flow and written to the cache so
        parallel/resumed flows on the same commit reuse it.
        """
        # Idempotent guard: already measured (None == not measured, [] == clean).
        if flow.state.baseline_failures is not None:
            return

        from .test_baseline import (
            BaselineCapture,
            compute_baseline_key,
            resolve_baseline_timeout,
            save_cache,
        )

        timeout = resolve_baseline_timeout(self.project_root)
        failures: Optional[set] = None
        background_timed_out = False

        capture = self._baseline_capture
        if capture is not None:
            try:
                # Bounded wait: a hung suite is killed at the bound rather than
                # blocking the flow forever before implement.
                failures = capture.wait_or_kill(timeout)
                background_timed_out = capture.timed_out
            except Exception as e:  # noqa: BLE001
                logger.warning("Baseline background capture wait failed: %s", e)
                failures = None

        # Synchronous fallback: no background handle, or the background run
        # signalled an infra failure via the None sentinel. Skip it after a
        # timeout — a hung suite would only hang again, doubling the wall-clock.
        if failures is None and not background_timed_out:
            logger.info(
                "Baseline not ready from background capture; running synchronous "
                "fallback measurement before implement"
            )
            try:
                failures = BaselineCapture(self.project_root).launch().wait_or_kill(timeout)
            except Exception as e:  # noqa: BLE001
                logger.warning("Synchronous baseline fallback failed: %s", e)
                failures = None

        if failures is None:
            # Both paths failed to produce a real measurement. Use an empty
            # baseline so introduced-failure detection still runs (the safe
            # direction); shout about it so the degradation is visible.
            logger.warning(
                "Could not measure a test baseline before implement; falling back "
                "to an EMPTY baseline. Pre-existing failures (if any) may be "
                "re-classified as introduced this run."
            )
            failures = set()

        flow.state.baseline_failures = sorted(failures)
        self.persistence.save_flow(flow)
        logger.info(
            "Baseline ready before implement: %d failing test(s)",
            len(flow.state.baseline_failures),
        )

        # Persist to the shared cache so concurrent/resumed flows on the same
        # commit reuse it. Best-effort: a cache-write failure must not block.
        try:
            key = self._baseline_key or compute_baseline_key(self.project_root)
            save_cache(self.project_root, key, set(failures))
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to save baseline cache: %s", e)

        # Drop the handle; subsequent re-entries hit the idempotent guard above.
        self._baseline_capture = None

    def cleanup_baseline_capture(self) -> None:
        """Terminate any still-running pre-implement baseline subprocess.

        The background baseline suite launched in :meth:`_start_baseline_capture`
        (from :meth:`init_flow`) is normally awaited and reaped by
        :meth:`_ensure_baseline_ready`, which the state machine invokes only just
        before the IMPLEMENT step. But a flow can terminate *before* IMPLEMENT is
        ever dispatched — analyze/plan/confirm failing, or the operator choosing
        Abort/Exit at a confirm pause — in which case ``_ensure_baseline_ready``
        never runs and the full-suite pytest would be left orphaned (and, for a
        genuinely hung test, never exit at all). The orchestrator calls this from
        its outermost ``finally`` so a pre-implement suite run can never outlive
        the flow.

        Idempotent and never raises: when no capture was launched (cache hit /
        resumed flow), or it was already reaped by ``_ensure_baseline_ready``,
        the handle is ``None`` and this is a no-op; a still-running subprocess is
        killed and reaped via :meth:`BaselineCapture.kill`.
        """
        capture = self._baseline_capture
        if capture is None:
            return
        try:
            capture.kill()
        except Exception as e:  # noqa: BLE001 — teardown must not crash shutdown
            logger.debug("Baseline capture cleanup failed: %s", e)
        finally:
            self._baseline_capture = None

    def get_progress(self, flow: FlowInstance) -> Dict[str, Any]:
        """Get detailed progress information.

        Args:
            flow: Flow instance

        Returns:
            Progress dictionary
        """
        completed, total = flow.get_progress()

        current_step = flow.state.get_current_step()

        plan_mode = PlanModeResolver.view(flow.state.context)

        return {
            "flow_id": flow.flow_id,
            "status": flow.status.value,
            "completed": completed,
            "total": total,
            "percent": (completed / total * 100) if total > 0 else 0,
            "current_step": current_step.step_type.value if current_step else None,
            "current_step_status": current_step.status.value if current_step else None,
            **plan_mode.to_projection(),
        }
