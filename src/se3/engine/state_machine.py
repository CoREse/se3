"""Core state machine implementation for the flow engine.

The StateMachine controls step transitions and execution flow.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

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
from .persistence import PersistenceManager
from ..config import insert_confirmation_steps, load_confirmation_config

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

        # Step handlers registry
        self._handlers: Dict[StepType, Callable[[Step, FlowInstance], Any]] = {}

        # Transition rules: (from_step, condition) -> to_step
        self._transitions: Dict[tuple[StepType, Optional[str]], StepType] = {}

        self._setup_default_transitions()

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
        is_loop_mode: bool = False,
    ) -> FlowInstance:
        """Create a new flow instance.

        Args:
            task_description: User's task description
            task_type: Type of task (feature, bugfix, review, etc.)
            change_name: Optional associated change name
            is_loop_mode: Whether this is a loop mode flow

        Returns:
            New flow instance
        """
        # Determine initial step sequence
        selected_steps = get_default_step_sequence(task_type)

        # Insert confirmation steps based on config
        selected_steps = self._insert_confirmation_steps(selected_steps)

        flow = FlowInstance(
            task_description=task_description,
            task_type=task_type,
            change_name=change_name,
            is_loop_mode=is_loop_mode,
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
        config = load_confirmation_config(self.project_root)
        if config.get("enabled", True):
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

        if existing and existing.status not in (FlowStatus.COMPLETED, FlowStatus.FAILED):
            # Found active flow - offer to resume
            return existing, True

        # No active flow - create new
        if not task_description:
            raise StateMachineError("No active flow and no task description provided")

        return self.create_flow(task_description, **kwargs), False

    def run_step(self, flow: FlowInstance, step: Step) -> StepStatus:
        """Execute a single step.

        Args:
            flow: Current flow instance
            step: Step to execute

        Returns:
            Final status of the step
        """
        handler = self._handlers.get(step.step_type)

        if not handler:
            logger.warning(f"No handler registered for {step.step_type.value}")
            step.status = StepStatus.FAILED
            step.error_message = f"No handler for step type {step.step_type.value}"
            return step.status

        # Mark as running
        step.status = StepStatus.RUNNING
        step.started_at = datetime.now()
        flow.status = FlowStatus.RUNNING
        self.persistence.save_flow(flow)

        logger.info(f"Running step: {step.step_type.value}")

        try:
            # Execute handler
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
                step.outputs["result"] = result

        except Exception as e:
            logger.exception(f"Step {step.step_type.value} failed")
            step.status = StepStatus.FAILED
            step.error_message = str(e)
            step.error_details = getattr(e, "__traceback__", None)

        finally:
            step.completed_at = datetime.now()
            self.persistence.save_flow(flow)

        logger.info(f"Step {step.step_type.value} finished with status: {step.status.value}")

        return step.status

    def transition_to_next(self, flow: FlowInstance) -> Optional[Step]:
        """Transition to the next step based on current state.

        Handles normal progression and review loop (going back to previous step).

        Args:
            flow: Current flow instance

        Returns:
            Next step if transition successful, None if flow complete
        """
        current_step = flow.state.get_current_step()

        if not current_step:
            raise TransitionError("No current step")

        # Check if current step completed successfully
        if current_step.status not in (StepStatus.COMPLETED, StepStatus.PAUSED, StepStatus.REVISION_NEEDED):
            logger.warning(
                f"Cannot transition from {current_step.status.value} step"
            )
            return None

        # Handle test-verify-fix loop: if VERIFY_SPEC returns REVISION_NEEDED
        if (
            current_step.step_type == StepType.VERIFY_SPEC
            and current_step.status == StepStatus.REVISION_NEEDED
        ):
            # Check if max iterations reached
            max_fix_iterations = self._get_max_fix_iterations()
            current_iteration = flow.state.get_fix_iteration()

            if current_iteration >= max_fix_iterations:
                logger.warning(
                    f"Max fix iterations ({max_fix_iterations}) reached, continuing to next step"
                )
                print(f"\n⚠️  Max fix iterations ({max_fix_iterations}) reached, proceeding...\n")
                # Fall through to normal progression - will go to next step
            else:
                fix_step = self._transition_to_fix(flow, current_step)
                if fix_step:
                    return fix_step
                # If transition failed, fall through to normal progression

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

        # Find next step in selected sequence
        selected = flow.state.selected_steps
        try:
            current_index = selected.index(current_step.step_type)
        except ValueError:
            raise TransitionError(f"Current step {current_step.step_type} not in selected sequence")

        if current_index >= len(selected) - 1:
            # Flow complete
            logger.info("Flow completed - all steps finished")
            flow.status = FlowStatus.COMPLETED
            flow.completed_at = datetime.now()
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

        # Reset the step for re-execution
        step_to_review.status = StepStatus.PENDING
        step_to_review.inputs["revision_feedback"] = feedback
        step_to_review.inputs["is_revision"] = True
        step_to_review.inputs["revision_iteration"] = iteration
        step_to_review.error_message = None
        step_to_review.error_details = None
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
        verify_step: Step,
    ) -> Optional[Step]:
        """Transition from VERIFY_SPEC back to IMPLEMENT for fixing issues.

        This implements the test-verify-fix loop. When tests fail or spec
        compliance issues are found, the verify_spec step returns REVISION_NEEDED
        and this method transitions back to the implement step with fix context.

        Args:
            flow: Current flow instance
            verify_step: The verify_spec step that detected issues

        Returns:
            The implement step being re-run, or None if failed
        """
        # Get fix instructions and context from verify step outputs
        fix_instructions = verify_step.outputs.get("fix_instructions", "")
        fix_context = verify_step.outputs.get("fix_context", {})
        fix_needed = verify_step.outputs.get("fix_needed", True)

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
        iteration = flow.state.increment_fix_iteration(
            fix_context={
                "verify_step_id": verify_step.step_id,
                "implement_step_id": implement_step.step_id,
                "reason": "test_failure" if fix_context.get("test_failed") else "spec_compliance",
            }
        )

        logger.info(
            f"Transitioning to fix iteration {iteration} for {implement_step.step_type.value}"
        )

        # Create a new implement step for the fix iteration
        # This ensures proper tracking and history
        new_implement_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.PENDING,
            inputs={
                "task_description": flow.task_description,
                "fix_instructions": fix_instructions,
                "fix_context": fix_context,
                "is_fix_iteration": True,
                "fix_iteration": iteration,
            },
        )
        
        # Copy relevant outputs from previous implement as reference
        prev_changes = implement_step.outputs.get("changes_made", {})
        if prev_changes:
            new_implement_step.inputs["previous_changes"] = prev_changes
        
        # Add the new step to flow
        flow.state.add_step(new_implement_step)
        flow.state.current_step_id = new_implement_step.step_id

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
        if fix_context.get("spec_issues"):
            print(f"Reason: Spec compliance issues found")
        print(f"Instructions: {fix_instructions[:200]}..." if len(fix_instructions) > 200 else f"Instructions: {fix_instructions}")
        print(f"{'='*60}\n")

        return implement_step

    def _get_max_fix_iterations(self) -> int:
        """Get the maximum number of fix iterations allowed.

        Returns:
            Maximum fix iterations (default: 3)
        """
        # Try to import from config, fallback to default
        try:
            from ..config import get_max_fix_iterations
            return get_max_fix_iterations(self.project_root)
        except ImportError:
            return 3

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

        # Load confirmation config for reviewer settings
        config = load_confirmation_config(self.project_root)

        # Gather outputs from previous steps
        for step_id in flow.state.step_history:
            step = flow.state.steps.get(step_id)
            if step and step.status == StepStatus.COMPLETED:
                # Add key outputs based on step type
                if step.step_type == StepType.DISCOVERY:
                    inputs["refined_description"] = step.outputs.get("refined_description")
                    inputs["discovery_summary"] = step.outputs.get("discovery_summary")
                elif step.step_type == StepType.ANALYZE:
                    inputs["task_type"] = step.outputs.get("task_type")
                    inputs["scope"] = step.outputs.get("scope")
                elif step.step_type == StepType.PROJECT_SUMMARY:
                    inputs["project_summary"] = step.outputs.get("project_summary")
                elif step.step_type == StepType.READ_SPEC:
                    inputs["relevant_specs"] = step.outputs.get("relevant_specs")
                    inputs["spec_content"] = step.outputs.get("spec_content")
                elif step.step_type == StepType.PROPOSE:
                    inputs["proposal"] = step.outputs.get("proposal")
                elif step.step_type == StepType.DESIGN:
                    inputs["design_doc"] = step.outputs.get("design_doc")
                    inputs["decisions"] = step.outputs.get("decisions")
                elif step.step_type == StepType.PLAN_TASKS:
                    inputs["task_groups"] = step.outputs.get("task_groups")
                    # Keep task_list for backward compatibility
                    inputs["task_list"] = step.outputs.get("task_list")
                elif step.step_type == StepType.IMPLEMENT:
                    # Build changes_made from implement outputs
                    inputs["changes_made"] = {
                        "files_changed": step.outputs.get("files_changed", []),
                        "implemented_groups": step.outputs.get("implemented_groups", []),
                    }
                elif step.step_type == StepType.TEST:
                    inputs["test_results"] = step.outputs.get("test_results")
                elif step.step_type == StepType.VERIFY_SPEC:
                    inputs["verification_result"] = step.outputs.get("verification_result")
                    inputs["fix_instructions"] = step.outputs.get("fix_instructions")
                    inputs["fix_context"] = step.outputs.get("fix_context")
                elif step.step_type == StepType.UPDATE_SPEC:
                    inputs["updated_specs"] = step.outputs.get("updated_specs")
                elif step.step_type == StepType.COMMIT:
                    inputs["commit_hash"] = step.outputs.get("commit_hash")
                elif step.step_type == StepType.CONFIRM:
                    # Pass through review result for tracking
                    inputs["last_review_result"] = step.outputs.get("review_result")

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
                            if s.outputs.get("step_to_review_id") == step_id:
                                already_confirmed = True
                                break
                    if not already_confirmed:
                        last_non_confirm_step = step
                        break

            if last_non_confirm_step:
                inputs["step_to_review_id"] = last_non_confirm_step.step_id
                inputs["step_to_review_type"] = last_non_confirm_step.step_type.value
                inputs["reviewer"] = config.get("reviewer", "human")

        # Special handling for IMPLEMENT step when in fix iteration
        if step_type == StepType.IMPLEMENT:
            # Check if we're in a fix loop
            fix_iteration = flow.state.get_fix_iteration()
            if fix_iteration > 0:
                # Include fix context for the implement step
                inputs["fix_iteration"] = fix_iteration
                inputs["fix_history"] = flow.state.fix_history

                # Find the most recent TEST and VERIFY_SPEC steps for context
                for step_id in reversed(flow.state.step_history):
                    step = flow.state.steps.get(step_id)
                    if step and step.step_type == StepType.TEST:
                        if "test_results" not in inputs:
                            inputs["test_results"] = step.outputs.get("test_results")
                    elif step and step.step_type == StepType.VERIFY_SPEC:
                        # Always use the most recent VERIFY_SPEC in fix loop
                        inputs["verification_result"] = step.outputs.get("verification_result")
                        inputs["fix_instructions"] = step.outputs.get("fix_instructions")
                        inputs["fix_context"] = step.outputs.get("fix_context")

        return inputs

    def run(self, flow: FlowInstance) -> FlowStatus:
        """Run the flow from current state to completion.

        Args:
            flow: Flow instance to run

        Returns:
            Final flow status
        """
        logger.info(f"Starting flow {flow.flow_id}")

        max_iterations = 100  # Safety limit
        iterations = 0

        while flow.status not in (FlowStatus.COMPLETED, FlowStatus.FAILED) and iterations < max_iterations:
            iterations += 1

            current_step = flow.state.get_current_step()

            if not current_step:
                logger.error("No current step in flow")
                flow.status = FlowStatus.FAILED
                break

            # Run current step
            step_status = self.run_step(flow, current_step)

            if step_status == StepStatus.FAILED:
                # Check if we should retry
                if current_step.retry_count < current_step.max_retries:
                    current_step.retry_count += 1
                    current_step.status = StepStatus.RETRYING
                    logger.info(f"Retrying step {current_step.step_type.value} (attempt {current_step.retry_count})")
                    continue
                else:
                    flow.status = FlowStatus.FAILED
                    break

            if step_status == StepStatus.PAUSED:
                # Flow paused for user input
                flow.status = FlowStatus.PAUSED
                logger.info("Flow paused - waiting for user input")
                break

            if step_status == StepStatus.REVISION_NEEDED:
                # Special case: revision was triggered
                # For CONFIRM steps, the handler already handled the transition
                # For VERIFY_SPEC steps, we need to call transition_to_next to trigger fix loop
                if current_step.step_type == StepType.CONFIRM:
                    logger.info("Revision triggered by CONFIRM, continuing flow")
                    continue
                # For VERIFY_SPEC, fall through to transition_to_next which will handle fix loop

            # Transition to next step
            next_step = self.transition_to_next(flow)

            if not next_step:
                # No more steps - flow complete
                break

        if iterations >= max_iterations:
            logger.error("Max iterations reached - possible infinite loop")
            flow.status = FlowStatus.FAILED

        # Final save
        self.persistence.save_flow(flow)

        logger.info(f"Flow {flow.flow_id} finished with status: {flow.status.value}")

        return flow.status

    def resume(self, flow: FlowInstance) -> FlowStatus:
        """Resume a paused flow.

        Args:
            flow: Flow instance to resume

        Returns:
            Final flow status
        """
        if flow.status != FlowStatus.PAUSED:
            logger.warning(f"Cannot resume flow with status {flow.status.value}")
            return flow.status

        logger.info(f"Resuming flow {flow.flow_id}")
        flow.status = FlowStatus.RUNNING

        return self.run(flow)

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
