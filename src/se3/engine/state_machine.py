"""Core state machine implementation for the flow engine.

The StateMachine controls step transitions and execution flow.
"""

from __future__ import annotations

import copy
import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

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
from .chat_history import _history_dir
from .llm_caller import clear_phase1_cache
from .token_usage import accumulate_step_usage, UsageTotals
from .issue_discovery import IssueDiscovery
from .issue_manager import IssueManager
from .persistence import PersistenceManager
from ..config import (
    ConfigError,
    insert_confirmation_steps,
    resolve_confirm_inputs,
    SpecLoadingConfig,
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
        "verify_spec": "spec_compliance",
    }
    # For unknown trigger types, return the trigger type itself rather than
    # silently mislabeling as "spec_compliance". Keeps future step types debuggable.
    return reason_map.get(trigger_step_type, trigger_step_type or "unknown")


def _effective_task_description_base(flow: "FlowInstance") -> str:
    """Pre-interjection base of the effective task_description.

    Returns ``flow.task_description`` unless a completed DISCOVERY step
    produced ``refined_description``, in which case the refined version
    overrides. Does NOT apply user_interjections — that's the
    ``_compose_effective_task_description`` step. Exposed separately so
    callers that need to RE-compose after appending an interjection
    (e.g. ``run.py:_handle_step_interrupt`` on a step whose inputs
    already carry a previously-composed task_description) can recover
    the un-decorated base without double-counting prior interjections
    that are already in the persisted list.
    """
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


def _compose_effective_task_description(flow: "FlowInstance") -> str:
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
    """
    base = _effective_task_description_base(flow)
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
    ) -> FlowInstance:
        """Create a new flow instance.

        Args:
            task_description: User's task description
            task_type: Type of task (feature, bugfix, review, etc.)
            change_name: Optional associated change name
            is_worktree_mode: Whether this flow runs in worktree isolation mode
                (``se3 run --worktree``)

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

        # Determine initial step sequence
        selected_steps = get_default_step_sequence(task_type)

        # Append optional steps from se3.yaml (e.g. summarize)
        selected_steps = self._apply_step_config(selected_steps)

        # Insert confirmation steps based on config
        selected_steps = self._insert_confirmation_steps(selected_steps)

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

    def _apply_step_config(self, steps: list[StepType]) -> list[StepType]:
        """Append optional steps from se3.yaml steps.append configuration.

        Args:
            steps: Original step sequence

        Returns:
            Modified step sequence with appended steps
        """
        from ..config import apply_step_config
        return apply_step_config(steps, self.project_root)

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

    def _spec_diff_guard_enabled(self, step: Step) -> bool:
        """Return True when the post-step spec-diff fallback guard applies to *step*.

        The guard (the second hard layer of the spec-write protection) catches a
        write to ``se3/specs/**`` that a step performed by going *around* the
        PreToolUse hook — most notably a ``Bash`` redirect / ``sed`` / ``tee``,
        which the tool-matcher hook (Write|Edit|NotebookEdit) never observes. It
        applies to every step NOT in the shared exemption set
        :data:`context_builder.SPEC_WRITE_ALLOWED_STEPS` (``update_spec`` + all
        sync steps ``sync_scan`` / ``sync_analyze`` / ``sync_resolve`` /
        ``sync_respond``), and only when
        ``spec_write_protection.diff_fallback_enabled`` is on.

        The exemption decision references the shared constant — never a bare
        ``step.step_type != UPDATE_SPEC`` literal — so the diff layer can never
        disagree with the soft-injection and PreToolUse-hook layers about which
        steps may legitimately write specs. Any config-loading fault degrades
        safely to *disabled* (the PreToolUse hook remains the primary guard).
        """
        try:
            from .context_builder import SPEC_WRITE_ALLOWED_STEPS

            if step.step_type.value in SPEC_WRITE_ALLOWED_STEPS:
                return False

            from ..config import load_spec_write_protection_config

            cfg = load_spec_write_protection_config(self.project_root)
            return bool(cfg.diff_fallback_enabled)
        except Exception:
            logger.debug(
                "Failed to resolve spec-diff guard for step '%s'",
                step.step_type.value,
                exc_info=True,
            )
            return False

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
        # so test/verify_spec can tell inherited (baseline) failures from
        # introduced ones. Idempotent across fix-loop re-entries into implement.
        if step.step_type == StepType.IMPLEMENT:
            self._ensure_baseline_ready(flow)

        # Mechanism A: capture the stable pre-update_spec spec snapshot before
        # UPDATE_SPEC's first edit, so SPEC_GATE can later tell edited from new
        # specs and enforce the requirement non-decrease invariant. Idempotent
        # across update_spec redos and fix-loop re-entries.
        if step.step_type == StepType.UPDATE_SPEC:
            self._snapshot_specs_before_update(flow)

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

        # Hard fallback layer (the second, post-hoc guard): snapshot every
        # se3/specs/** file's content hash before a non-exempt step runs, so a
        # within-step spec write that slipped past the PreToolUse hook (most
        # notably a Bash redirect / sed / tee, which the tool-matcher hook never
        # sees) can be detected after the handler returns. This guard only asks
        # "did this step touch a spec file at all" — it is wholly orthogonal to
        # verify_spec's in_scope/out_of_scope judgement and never inspects spec
        # content semantics. ``update_spec`` and every sync step are exempt via
        # the shared SPEC_WRITE_ALLOWED_STEPS set (see _spec_diff_guard_enabled).
        # We capture full byte content (not just hashes) so the post-step guard
        # can REVERT an illegal write, not merely flag it — a left-on-disk spec
        # change would otherwise survive a later `se3 run --resume` and leak
        # through to commit.
        spec_guard_before: Optional[Dict[str, bytes]] = None
        if self._spec_diff_guard_enabled(step):
            try:
                from .spec_write_hook import capture_spec_contents

                spec_guard_before = capture_spec_contents(self.project_root)
            except Exception:
                logger.debug(
                    "Failed to snapshot specs before step '%s'",
                    step.step_type.value,
                    exc_info=True,
                )
                spec_guard_before = None

        # Step-scoped token-usage accumulator. Opened before the handler runs so
        # every LLM subprocess call made during this step (main call, retry,
        # rotation, two-phase JSON extraction) folds into one per-step total via
        # token_usage.add_call_usage. The yielded UsageTotals is captured here so
        # the finally block can read it even after the context manager has reset
        # the contextvar on exit (including the exception path).
        step_usage = None
        try:
            # Execute handler under the step usage scope.
            with accumulate_step_usage() as step_usage:
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

            # Hard fallback layer: if a non-exempt step wrote any se3/specs/**
            # file (detected by content-hash diff against the pre-step snapshot),
            # fail the step. This backstops the PreToolUse hook against a Bash
            # redirect / sed / tee that the tool-matcher hook never observes. We
            # only override a non-FAILED status: a handler that already failed
            # for some other reason keeps its own error (and on that path the
            # spec write, if any, was already a side effect of a failing step).
            # The exemption decision routes through SPEC_WRITE_ALLOWED_STEPS, so
            # update_spec / all sync steps are never flagged. Best-effort: a
            # fault in the check must never break the step.
            if spec_guard_before is not None and step.status != StepStatus.FAILED:
                try:
                    from .spec_write_hook import (
                        capture_spec_contents,
                        diff_spec_files,
                        restore_spec_files,
                    )

                    spec_guard_after = capture_spec_contents(self.project_root)
                    changed = diff_spec_files(spec_guard_before, spec_guard_after)
                    if changed:
                        step.status = StepStatus.FAILED
                        # Revert the illegal write so it cannot persist on disk:
                        # restore each touched file to its pre-step content (or
                        # delete a newly-created one). Without this, a left-on-disk
                        # spec change survives a later `se3 run --resume` (the
                        # resumed pre-step snapshot already holds the tampered
                        # content, the re-run diffs clean, and the change reaches
                        # commit).
                        revert_failed = restore_spec_files(
                            self.project_root, spec_guard_before, changed
                        )
                        revert_note = (
                            " The illegal spec change has been reverted to its "
                            "pre-step state."
                            if not revert_failed
                            else (
                                " WARNING: could not revert spec file(s): "
                                f"{', '.join(revert_failed)}; remove the change "
                                "manually before continuing."
                            )
                        )
                        step.error_message = (
                            f"Step '{step.step_type.value}' illegally modified "
                            f"spec file(s) under se3/specs/: "
                            f"{', '.join(changed)}. Writing spec files is the "
                            f"dedicated responsibility of the update_spec step "
                            f"and `se3 sync`; no other step may create, modify, "
                            f"or delete spec files. Changing existing code "
                            f"behavior IS allowed — declare any needed spec "
                            f"change through the plan spec_changes channel "
                            f"(handled by verify_spec / update_spec) rather than "
                            f"editing spec files in this step." + revert_note
                        )
                        logger.error(step.error_message)
                except Exception:
                    logger.debug(
                        "Spec-diff fallback guard check failed for step '%s'",
                        step.step_type.value,
                        exc_info=True,
                    )

            # Aggregate this step's token usage before persisting. Best-effort:
            # a fault here must never break the step / flow.
            #
            # Two consumers read this:
            #  * the CLI session total — flow.state.session_token_usage — which
            #    folds EVERY run_step's usage (this is the authoritative total);
            #  * the web session badge — which re-derives the session total by
            #    summing token_usage off the *emitted* terminal step_completed
            #    records (one per COMPLETED/PARTIAL/FAILED run; see run.py).
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
            # To keep the web session badge (which re-derives the total by
            # summing token_usage off emitted terminal records) in agreement
            # with the CLI authoritative total (session_token_usage, which
            # folds EVERY run_step's step_usage), a non-terminal run also
            # carries the combined total forward in `carried_token_usage` so
            # the next emitted terminal record's token_usage includes all prior
            # non-emitting rounds. The session total still adds only the
            # current run's step_usage — not the combined total — so there is
            # no double-counting.
            try:
                if step_usage is not None and not step_usage.is_empty():
                    flow.state.session_token_usage.add(step_usage)
                # Combine this run's usage with usage carried from prior
                # non-terminal (PAUSED / REVISION_NEEDED) runs of this step.
                combined = UsageTotals.from_dict(
                    step.outputs.get("carried_token_usage")
                )
                if step_usage is not None:
                    combined.add(step_usage)
                # Publish `token_usage` for both terminal and non-terminal
                # runs so step-level renderers can display it.
                if not combined.is_empty():
                    step.outputs["token_usage"] = combined.to_dict()
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
                    if not combined.is_empty():
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

    def transition_to_next(self, flow: FlowInstance) -> Optional[Step]:
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

        # Handle the fix loop: TEST, SELF_CHECK, VERIFY_SPEC, or the mechanism-A
        # SPEC_GATE returning REVISION_NEEDED. All four share the same global
        # max_fix_iterations exhaustion bound; SPEC_GATE differs only in WHERE it
        # routes (gate_route: implement → fix loop, update_spec → redo).
        if (
            current_step.step_type in (
                StepType.TEST,
                StepType.SELF_CHECK,
                StepType.VERIFY_SPEC,
                StepType.SPEC_GATE,
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
                print(f"\n❌  Fix loop exhausted after {max_fix_iterations} iterations. Flow stopped.\n")
                # A-class trigger: create issue for fix loop exhaustion
                try:
                    discovery = self._get_issue_discovery(flow)
                    if discovery:
                        discovery.create_from_fix_loop_exhaustion(flow, current_step)
                except Exception as e:
                    logger.warning(f"Failed to create fix-loop exhaustion issue: {e}")
                flow.status = FlowStatus.FAILED
                return None
            elif current_step.step_type == StepType.SPEC_GATE:
                # SPEC_GATE dispatch by gate_route. An invalid spec artifact
                # routes back to update_spec for a redo; an introduced test
                # failure after the spec edit routes to the implement fix loop.
                gate_route = current_step.outputs.get("gate_route", "")
                if gate_route == "update_spec":
                    redo_step = self._transition_to_update_spec_redo(flow, current_step)
                    if redo_step:
                        return redo_step
                    logger.info("update_spec redo returned None, falling through to next step")
                else:
                    # gate_route == "implement" (or unset → default to the fix loop)
                    fix_step = self._transition_to_fix(flow, current_step)
                    if fix_step:
                        return fix_step
                    logger.info("Fix transition returned None, falling through to next step")
            else:
                fix_step = self._transition_to_fix(flow, current_step)
                if fix_step:
                    return fix_step
                # No implement step found — fall through to normal progression
                logger.info("Fix transition returned None, falling through to next step")

        # Handle review loop: if current step is CONFIRM and revision was requested
        if current_step.step_type == StepType.CONFIRM:
            review_result = current_step.outputs.get("review_result", {})
            approved = review_result.get("approved", True)

            if approved:
                # Approval received - continue to next step
                logger.info(f"Confirmation approved for {review_result.get('step_to_review_type', 'unknown')}")
            else:
                # Revision requested - go back to the step being reviewed
                # Get step_to_review_id from review_result (set by confirm_handler)
                step_to_review_id = review_result.get("step_to_review_id")
                revision_step = self._transition_to_revision(flow, current_step, step_to_review_id)
                if revision_step:
                    return revision_step
                # If transition failed, continue to normal flow (will likely fail later)

        # Handle N-pass self_check: if SELF_CHECK completed, check if we need more passes
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
            passes_required = self._get_self_check_passes_required()
            consecutive_passes = self._count_consecutive_self_check_completed(flow)

            if consecutive_passes < passes_required:
                next_pass_index = consecutive_passes + 1
                logger.info(
                    f"Self-check pass {consecutive_passes}/{passes_required} completed; "
                    f"creating repeat pass #{next_pass_index}/{passes_required}"
                )
                repeat_step = self._create_self_check_repeat_step(flow)
                return repeat_step
            # else: all N passes completed — fall through to normal progression

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

        # Update flow state to point back to this step
        flow.state.current_step_id = step_to_review_id

        # Find the index of this step type in selected_steps
        try:
            step_index = flow.state.selected_steps.index(step_to_review.step_type)
            flow.state.current_step_index = step_index
        except ValueError:
            logger.warning(f"Step type {step_to_review.step_type} not in selected sequence")

        self.persistence.save_flow(flow)

        print(f"\n{'='*60}")
        print(f"🔁 REVISION REQUESTED: {step_to_review.step_type.value.upper()}")
        print(f"{'='*60}")
        print(f"Iteration: {iteration}")
        print(f"Feedback: {feedback[:200]}..." if len(feedback) > 200 else f"Feedback: {feedback}")
        print(f"{'='*60}\n")

        return step_to_review

    def _transition_to_fix(
        self,
        flow: FlowInstance,
        trigger_step: Step,
    ) -> Optional[Step]:
        """Transition from TEST, SELF_CHECK, or VERIFY_SPEC back to IMPLEMENT for fixing issues.

        This implements the test-selfcheck-verify-fix loop. When issues are detected
        (by TEST, SELF_CHECK, or VERIFY_SPEC step), the step returns REVISION_NEEDED
        and this method transitions back to the implement step with fix context.

        Args:
            flow: Current flow instance
            trigger_step: The step (TEST, SELF_CHECK, or VERIFY_SPEC) that detected issues

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
        print(f"🔧 FIX LOOP: RETURNING TO IMPLEMENT STEP")
        print(f"{'='*60}")
        print(f"Iteration: {iteration}")
        if fix_context.get("test_failed"):
            print(f"Reason: Tests failed")
        if trigger_step_type == "self_check":
            print(f"Source: self_check (code review)")
        if trigger_step_type == "verify_spec":
            print(f"Source: verify_spec (spec compliance check)")
        if fix_context.get("reason") == "self_check":
            print(f"Reason: Code review found actionable issues")
        if fix_context.get("spec_issues"):
            print(f"Reason: Spec compliance issues found")
        print(f"Instructions: {fix_instructions[:200]}..." if len(fix_instructions) > 200 else f"Instructions: {fix_instructions}")
        print(f"{'='*60}\n")

        return implement_step

    def _transition_to_update_spec_redo(
        self,
        flow: FlowInstance,
        gate_step: Step,
    ) -> Optional[Step]:
        """Route SPEC_GATE → UPDATE_SPEC for a redo when the spec artifact is invalid.

        Mechanism A, invalid-artifact branch: ``update_spec`` produced a
        structurally broken / requirement-deleting spec. Re-run ``update_spec``
        with the gate's fix instructions (which name the structural / requirement
        problems) so the redo repairs the artifact; normal progression then sends
        the flow back into SPEC_GATE to re-check the redone spec.

        Counts toward the shared global ``fix_iterations`` so the redo loop is
        bounded by ``max_fix_iterations`` exactly like the implement fix loop —
        a spec the LLM can never make valid cannot spin forever.

        Args:
            flow: Current flow instance.
            gate_step: The SPEC_GATE step that flagged the invalid artifact.

        Returns:
            The ``update_spec`` step reset for re-execution, or None if no
            ``update_spec`` step is found / no fix is needed.
        """
        fix_instructions = gate_step.outputs.get("fix_instructions", "")
        fix_context = gate_step.outputs.get("fix_context", {})
        fix_needed = gate_step.outputs.get("fix_needed", True)

        if not fix_needed:
            logger.warning("update_spec redo called but fix_needed is False")
            return None

        # Find the most recent update_spec step in history.
        update_step: Optional[Step] = None
        for step_id in reversed(flow.state.step_history):
            step = flow.state.steps.get(step_id)
            if step and step.step_type == StepType.UPDATE_SPEC:
                update_step = step
                break

        if not update_step:
            logger.warning("No update_spec step found for spec_gate redo transition")
            return None

        # Charge the shared global fix-iteration counter so the redo loop shares
        # the same exhaustion bound as the implement fix loop.
        iteration = flow.state.increment_fix_iteration(
            fix_context={
                "trigger_step_id": gate_step.step_id,
                "trigger_step_type": gate_step.step_type.value,
                "update_spec_step_id": update_step.step_id,
                "reason": fix_context.get("reason") or "spec_artifact",
                "issues": _normalize_issue_fields(
                    copy.deepcopy(_cap_issue_list(fix_context.get("issues", [])))
                ),
            }
        )

        logger.info(
            f"Transitioning to update_spec redo (fix iteration {iteration}); "
            f"spec artifact invalid"
        )

        # Clear any Phase 1 cache — a redo is a full fresh LLM call.
        clear_phase1_cache(self.project_root, flow.flow_id, update_step.step_id)

        # Reset the existing update_spec step for re-execution, injecting the
        # gate's fix instructions/context so the redo knows WHICH requirement was
        # deleted / which structural rule was violated.
        update_step.status = StepStatus.PENDING
        update_step.inputs["fix_instructions"] = fix_instructions
        update_step.inputs["fix_context"] = fix_context
        update_step.inputs["is_spec_redo"] = True
        update_step.inputs["fix_iteration"] = iteration
        # A redo is a NEW LLM call with its own fix prompt, not a retry of the
        # prior update_spec call. Clear any stale retry counter.
        _reset_retry_counter_for_new_call(update_step)
        update_step.outputs["_is_outdated"] = True

        # Point the flow back at update_spec; normal progression after it
        # completes lands on the SPEC_GATE that follows it in the sequence.
        flow.state.current_step_id = update_step.step_id
        try:
            step_index = flow.state.selected_steps.index(StepType.UPDATE_SPEC)
            flow.state.current_step_index = step_index
        except ValueError:
            logger.warning("UPDATE_SPEC step type not in selected sequence")

        self.persistence.save_flow(flow)

        print(f"\n{'='*60}")
        print(f"📝 SPEC GATE: REDOING UPDATE_SPEC (invalid spec artifact)")
        print(f"{'='*60}")
        print(f"Iteration: {iteration}")
        print(
            f"Instructions: {fix_instructions[:200]}..."
            if len(fix_instructions) > 200
            else f"Instructions: {fix_instructions}"
        )
        print(f"{'='*60}\n")

        return update_step

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

        Memoized on the instance to avoid re-reading se3.yaml within a
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
           The user must fix se3.yaml before the flow starts.
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
                "se3.yaml to clear the warning.",
                e,
            )
            cfg = last_good
        self._workflow_config_cache = cfg
        self._workflow_config_last_good = cfg
        return cfg

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
        # computed by the shared helper so the state machine and ``se3 history
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

    def _create_self_check_repeat_step(self, flow: FlowInstance) -> Step:
        """Create a repeated self_check Step instance for the N-pass requirement.

        Builds inputs via _build_step_inputs which computes the pass position so
        the handler can log ``#i/N`` and the inputs carry the right metadata.

        Args:
            flow: Current flow instance.

        Returns:
            A new Step instance of type SELF_CHECK, already added to flow.state.
        """
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
                    # New outputs merged from former project_summary and read_spec steps
                    inputs["project_summary"] = step.outputs.get("project_summary")
                    inputs["relevant_specs"] = step.outputs.get("relevant_specs")
                    inputs["spec_content"] = step.outputs.get("spec_content")
                    inputs["selected_items"] = step.outputs.get("selected_items", [])
                # Deprecated: PROJECT_SUMMARY merged into ANALYZE (backward compat for persisted flows)
                elif step.step_type == StepType.PROJECT_SUMMARY:
                    inputs["project_summary"] = step.outputs.get("project_summary")
                elif step.step_type == StepType.PLAN:
                    plan = step.outputs.get("plan", {})
                    inputs["proposal"] = plan.get("proposal", {})
                    inputs["design_doc"] = plan.get("design", {})
                    inputs["task_groups"] = step.outputs.get("task_groups")
                    inputs["spec_changes"] = step.outputs.get("spec_changes", [])
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
                elif step.step_type == StepType.VERIFY_SPEC:
                    inputs["verification_result"] = step.outputs.get("verification_result")
                    inputs["fix_instructions"] = step.outputs.get("fix_instructions")
                    inputs["fix_context"] = step.outputs.get("fix_context")
                elif step.step_type == StepType.UPDATE_SPEC:
                    inputs["updated_specs"] = step.outputs.get("updated_specs")
                elif step.step_type == StepType.VERSION_ANALYZE:
                    inputs["bump_type"] = step.outputs.get("bump_type")
                    inputs["reasoning"] = step.outputs.get("reasoning")
                    inputs["confidence"] = step.outputs.get("confidence")
                    inputs["suggested_version"] = step.outputs.get("suggested_version")
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

        # Resolve spec loading mode for downstream steps.
        # If the current step is configured for full_spec mode, re-render
        # spec_content from the full spec files rather than using the
        # analyze-step item-filtered version.
        #
        # Sentinel check: distinguish "ANALYZE never ran" (key absent) from
        # "ANALYZE ran and returned an empty list" (key present, value []).  The
        # old truthy check `if not selected_items` would fall back to stale
        # context for an explicit empty list, pulling in specs from a previous
        # ANALYZE run that the user did not intend.
        if "selected_items" not in inputs:
            # ANALYZE step hasn't populated it — fall back to flow context
            # (e.g. flow resumed mid-way, CONFIRM steps obscure the walk).
            fallback = flow.state.context.get("selected_items")
            if fallback is not None:
                selected_items = fallback
            else:
                selected_items = []
            inputs["selected_items"] = selected_items
        else:
            selected_items = inputs["selected_items"] or []

        try:
            from ..config import load_spec_loading_config
            spec_loading = load_spec_loading_config(self.project_root)
            load_mode = spec_loading.mode_for(step_type.value)
        except Exception:
            # Fall back to per-step built-in defaults (items for every step
            # unless a project explicitly opts a step into full_spec) when
            # config loading fails.
            load_mode = SpecLoadingConfig().mode_for(step_type.value)

        if load_mode == "full_spec":
            from .spec_loader import load_for_step
            try:
                full_result = load_for_step(
                    step_type=step_type.value,
                    selected_items=selected_items,
                    project_root=self.project_root,
                    mode="full_spec",
                )
            except ValueError:
                # Configuration/data errors (e.g. empty selected_items in
                # full_spec mode) must surface immediately — they indicate a
                # broken upstream step, not a recoverable I/O hiccup.
                raise
            except Exception:
                logger.warning(
                    "full_spec load failed for step %s; "
                    "downstream step receiving items-mode spec_content",
                    step_type.value,
                    exc_info=True,
                )
                # Leave the existing items-mode spec_content in place
            else:
                inputs["spec_content"] = full_result.text
                inputs["relevant_specs"] = full_result.relevant_specs

        # Ensure selected_items is always present in inputs (may be empty)
        inputs["selected_items"] = selected_items

        # When discovery produced a refined_description, preserve original
        # for traceability and override the effective task_description.
        # Then apply any persisted user-Ctrl-C interjections.
        # ``_compose_effective_task_description`` encapsulates the same
        # priority chain (refined > original, then interjections) used by
        # ``_transition_to_fix`` so the two paths cannot diverge.
        if "refined_description" in inputs and inputs["refined_description"]:
            inputs["original_task_description"] = inputs["task_description"]
        inputs["task_description"] = _compose_effective_task_description(flow)

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

                # Single YAML read for the entire CONFIRM resolution —
                # consolidates what used to be three separate config
                # reads (load_confirmation_config + load_agent_registry
                # + load_agents) per step transition.
                resolved = resolve_confirm_inputs(
                    self.project_root, reviewed_type,
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
            # Compute pass index: consecutive COMPLETED self_check at tail + 1
            pass_index = self._count_consecutive_self_check_completed(flow) + 1
            workflow_cfg = self._get_workflow_config()
            inputs["self_check_pass_index"] = pass_index
            inputs["self_check_passes_required"] = self._get_self_check_passes_required()
            inputs["self_check_convergence_enabled"] = workflow_cfg.self_check_convergence_enabled
            inputs["self_check_defer_fix_threshold"] = workflow_cfg.self_check_defer_fix_threshold

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
            # se3.yaml otherwise). Treat the input as a hard contract: the
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

            # Inject prev_self_check_issues whenever this is the first pass
            # of a fix-loop round (pass_index == 1 AND fix_iteration > 0).
            # The earlier ``self_check_convergence_enabled`` gate has been
            # dropped: with the new schema (verbatim_quote + evidence_lines
            # + previous_issue_resolutions), the LLM is required to
            # explicitly declare each prev_issue as fixed/still_present, so
            # passing prev_issues is essential for correct review whether or
            # not the fuzzy ``_issues_converged`` shortcut is enabled.
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

        # Special handling for VERIFY_SPEC step when in fix iteration
        if step_type == StepType.VERIFY_SPEC:
            # Inject the same frozen baseline as the TEST step so verify_spec's
            # test gate consumes the identical inherited-vs-introduced verdict
            # (not-yet-captured injects as []).
            inputs["baseline_failures"] = list(flow.state.baseline_failures or [])
            # Always populate max_fix_iterations (see SELF_CHECK comment above
            # for rationale: avoids extra YAML parse on the initial pass).
            inputs["max_fix_iterations"] = self._get_max_fix_iterations()
            fix_iteration = flow.state.get_fix_iteration()
            if fix_iteration > 0:
                inputs["fix_iteration"] = fix_iteration
                inputs["fix_history"] = copy.deepcopy(flow.state.fix_history)
                # Find previous VERIFY_SPEC with REVISION_NEEDED
                for step_id in reversed(flow.state.step_history):
                    step = flow.state.steps.get(step_id)
                    if not step or step.status == StepStatus.FAILED:
                        continue
                    if (step.step_type == StepType.VERIFY_SPEC
                            and step.status == StepStatus.REVISION_NEEDED):
                        inputs["prev_verification_result"] = copy.deepcopy(
                            step.outputs.get("verification_result")
                        )
                        inputs["prev_fix_instructions"] = step.outputs.get("fix_instructions") or ""
                        all_issues = step.outputs.get("issues", [])
                        inputs["prev_issues"] = copy.deepcopy(all_issues[:20])
                        break

        # Special handling for the mechanism-A SPEC_GATE step. It re-runs the
        # full test suite through the same shared core as TEST, so it needs the
        # identical frozen baseline (inherited-vs-introduced split) the TEST step
        # gets. It also needs the stable pre-update_spec requirement snapshot to
        # detect edited specs and enforce the non-decrease invariant.
        # tests_added / estimated_test_duration are already forwarded from the
        # IMPLEMENT outputs in the history loop above; mirror them defensively
        # so the gate still has them even if implement is missing from history.
        if step_type == StepType.SPEC_GATE:
            inputs["baseline_failures"] = list(flow.state.baseline_failures or [])
            inputs["spec_requirement_baseline"] = flow.state.context.get(
                "spec_requirement_baseline", {}
            )
            inputs.setdefault("tests_added", [])
            inputs.setdefault("estimated_test_duration", None)

        # Special handling for IMPLEMENT step when in fix iteration
        if step_type == StepType.IMPLEMENT:
            # Check if we're in a fix loop
            fix_iteration = flow.state.get_fix_iteration()
            if fix_iteration > 0:
                # Include fix context for the implement step
                inputs["fix_iteration"] = fix_iteration
                inputs["fix_history"] = copy.deepcopy(flow.state.fix_history)

                # Find the most recent TEST and VERIFY_SPEC steps for context
                found_test = False
                found_verify = False
                for step_id in reversed(flow.state.step_history):
                    step = flow.state.steps.get(step_id)
                    if not step or step.status == StepStatus.FAILED:
                        continue
                    if step.step_type == StepType.TEST and not found_test:
                        # Always overwrite with a deep copy in the fix loop —
                        # the base-loop version is a direct reference, which would
                        # leak later mutations to step.outputs.
                        inputs["test_results"] = copy.deepcopy(
                            step.outputs.get("test_results")
                        )
                        found_test = True
                    elif step.step_type == StepType.VERIFY_SPEC and not found_verify:
                        inputs["verification_result"] = copy.deepcopy(
                            step.outputs.get("verification_result")
                        )
                        inputs["fix_instructions"] = step.outputs.get("fix_instructions") or ""
                        inputs["fix_context"] = copy.deepcopy(
                            step.outputs.get("fix_context")
                        )
                        found_verify = True
                    if found_test and found_verify:
                        break

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

        self._write_flow_meta(flow)
        self._record_baseline_commit(flow)
        self._start_baseline_capture(flow)

    def _snapshot_specs_before_update(self, flow: FlowInstance) -> None:
        """Capture the stable pre-``update_spec`` spec snapshot, once per flow.

        Mechanism A: before ``update_spec`` first edits any spec, record each
        on-disk spec's full content plus its ``### Requirement:`` name set into
        ``flow.state.context['spec_requirement_baseline']``. SPEC_GATE diffs the
        current disk state against this snapshot to split edited vs new specs and
        to enforce the requirement non-decrease invariant on edited specs.

        Captured a **single time** per flow and never overwritten: re-snapshotting
        before an ``update_spec`` redo (or a fix-loop re-entry) would let the gate
        measure non-decrease against an already-corrupted baseline and wave a
        deletion through. The one-shot guard keys off the context key's presence,
        so a legitimately empty snapshot (no specs on disk / missing specs dir) is
        still recorded once and not re-taken.

        Never raises: a snapshot failure must not crash the flow (the gate
        degrades to a skip when the snapshot is absent).
        """
        if "spec_requirement_baseline" in flow.state.context:
            return
        try:
            from .steps.spec_gate import build_spec_requirement_baseline

            snapshot = build_spec_requirement_baseline(self.project_root)
            flow.state.context["spec_requirement_baseline"] = snapshot
            self.persistence.save_flow(flow)
            logger.info(
                "spec_gate: captured pre-update_spec snapshot of %d spec(s)",
                len(snapshot),
            )
        except Exception as e:  # noqa: BLE001 — never crash the flow on snapshot setup
            logger.warning("Failed to capture pre-update_spec spec snapshot: %s", e)

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

        return {
            "flow_id": flow.flow_id,
            "status": flow.status.value,
            "completed": completed,
            "total": total,
            "percent": (completed / total * 100) if total > 0 else 0,
            "current_step": current_step.step_type.value if current_step else None,
            "current_step_status": current_step.status.value if current_step else None,
        }
