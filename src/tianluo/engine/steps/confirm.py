"""Confirm step handler for human and LLM review."""

from tianluo.runtime_paths import runtime_dir
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..context_builder import (
    build_adjudicate_review_context,
    build_confirm_prompt,
    build_llm_review_prompt,
    build_plan_confirm_prompt,
)
from ..interaction_calls import CALL_KIND_CONFIRM
from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus, StepType
from ._project_root import resolve_flow_project_root
from ..utils.json_parser import parse_json_response

logger = logging.getLogger(__name__)


class TimeoutError(Exception):
    """Raised when waiting for human response times out."""
    pass


def _check_existing_response(call_file_path: Path) -> Optional[Dict[str, Any]]:
    """Check if a response file already exists for the given call file.

    This is used on resume to detect if human has already responded.

    Args:
        call_file_path: Path to the original call file

    Returns:
        Dict with 'approved' and 'feedback' if response exists, None otherwise
    """
    # Response file has .response suffix
    response_path = call_file_path.parent / f"{call_file_path.stem}.response"

    if not response_path.exists():
        return None

    try:
        with open(response_path, 'r') as f:
            data = json.load(f)

        return {
            'approved': data.get('approved', False),
            'feedback': data.get('feedback'),
            'step_to_review_id': data.get('step_to_review_id'),
            'step_to_review_type': data.get('step_to_review_type'),
        }

    except (json.JSONDecodeError, KeyError, IOError) as e:
        logger.error(f"Error reading response file {response_path}: {e}")
        return None


def _wait_for_human_response(
    call_file_path: Path,
    timeout: Optional[float] = None,
    poll_interval: float = 1.0
) -> Dict[str, Any]:
    """Wait for human response by polling for response file.

    Args:
        call_file_path: Path to the call file
        timeout: Maximum time to wait in seconds (None = no timeout)
        poll_interval: Seconds between polls

    Returns:
        Dict with 'approved' and 'feedback' when response is received

    Raises:
        TimeoutError: If timeout is reached without response
    """
    start_time = time.time()

    while True:
        # Check for existing response
        response = _check_existing_response(call_file_path)
        if response is not None:
            return response

        # Check timeout
        if timeout is not None:
            elapsed = time.time() - start_time
            if elapsed >= timeout:
                raise TimeoutError(f"Timeout waiting for human response after {timeout}s")

        # Sleep before next poll
        time.sleep(poll_interval)


def _create_call_file(step: Step, flow: FlowInstance, project_root: Path) -> Path:
    """Create a call file for human review.

    Args:
        step: The current CONFIRM step
        flow: The flow instance
        project_root: Project root directory

    Returns:
        Path to created call file
    """
    calls_dir = runtime_dir(project_root) / "calls"
    calls_dir.mkdir(parents=True, exist_ok=True)

    # Use timestamp and step name for unique filename
    timestamp = int(time.time())
    change_id = flow.change_name or flow.flow_id
    call_file = calls_dir / f"confirm_{step.step_id}_{timestamp}.json"

    # Get the step being reviewed
    step_to_review_id = step.inputs.get('step_to_review_id')
    step_to_review_type = step.inputs.get('step_to_review_type', 'unknown')

    context: Dict[str, Any] = {
        'flow_id': flow.flow_id,
        'step_id': step.step_id,
        'step_to_review_type': step_to_review_type,
        'step_to_review_id': step_to_review_id,
    }

    # An adjudicate approval gate is un-actionable without the ruling in view:
    # inject the rationale, the pre-ruling baseline vs. post-ruling description
    # (so the web console can render a before/after diff), and the boundary
    # clause's `covered_surfaces` — the surfaces it claims to govern, each with
    # its by-construction justification, which is what lets a human catch a
    # wrongly-swept sibling surface before it is written into the contract.
    # Non-adjudicate confirms carry none of these fields (helper returns {}),
    # keeping their payload unchanged.
    if step_to_review_type == 'adjudicate':
        context.update(build_adjudicate_review_context(flow, step_to_review_id))

    call_data = {
        'step': step.step_id,
        'step_id': step.step_id,
        'change_id': change_id,
        'flow_id': flow.flow_id,
        'step_to_review_type': step_to_review_type,
        'step_to_review_id': step_to_review_id,
        'timestamp': timestamp,
        # ``type`` is the legacy field kept for backward compatibility; ``kind``
        # is the canonical CALL_KIND_* discriminant the daemon aggregator / web
        # console dispatch on to render the Approve/Reject UI.
        'type': 'confirm',
        'kind': CALL_KIND_CONFIRM,
        'prompt': build_confirm_prompt(step_to_review_type),
        'context': context,
    }

    with open(call_file, 'w') as f:
        json.dump(call_data, f, indent=2, ensure_ascii=False)

    logger.info(f"Created confirmation call file: {call_file}")
    return call_file


def _llm_review(step: Step, flow: FlowInstance) -> Tuple[StepStatus, Dict[str, Any]]:
    """Perform LLM-based review of a step's output.

    Retrieves the reviewed step's output, builds a review prompt, calls the LLM,
    and returns the appropriate status and review result.

    Reads ``step.inputs['agents']`` (resolved by ``state_machine`` from
    either the per-step ``reviewer: <agent_name>`` reference or the
    ``llm_caller.defaults`` fallback chain) and passes it to
    ``LLMCaller`` explicitly so this handler does not re-resolve the
    chain on its own.

    Args:
        step: The current CONFIRM step
        flow: The flow instance

    Returns:
        Tuple of (StepStatus, review_result dict)
    """
    step_to_review_id = step.inputs.get('step_to_review_id')
    step_to_review_type = step.inputs.get('step_to_review_type', 'unknown')
    max_iterations = step.inputs.get('max_iterations', 3) or 3
    agents = step.inputs.get('agents')

    # Iteration cap is driven by the persisted, cross-revision review
    # counter on flow.state, NOT a per-confirm counter. Each revision
    # creates a brand-new CONFIRM step (state_machine.transition_to_next),
    # so the old per-confirm step.inputs['_llm_review_iteration'] reset to
    # 0 every cycle and the max_iterations safety net never engaged. The
    # persisted counter is incremented once per revision by
    # _transition_to_revision (flow.state.increment_review_iteration), so
    # reading it here makes the cap actually bound the review<->revise loop
    # across the new-confirm chain.
    if step_to_review_id is not None:
        review_iteration = flow.state.get_review_iteration(step_to_review_id)
    else:
        # Degenerate confirm with no reviewed step — fall back to the
        # legacy per-confirm counter so behavior stays safe (no crash).
        review_iteration = step.inputs.get('_llm_review_iteration', 0)

    # Check max iterations — auto-approve if exceeded
    if review_iteration >= max_iterations:
        logger.warning(
            f"LLM review max iterations ({max_iterations}) reached for "
            f"{step_to_review_type}, auto-approving"
        )
        review_result = {
            'approved': True,
            'feedback': f'Auto-approved: max review iterations ({max_iterations}) reached.',
            'step_to_review_id': step_to_review_id,
            'step_to_review_type': step_to_review_type,
            'reviewer': 'llm',
        }
        return StepStatus.COMPLETED, review_result

    # Get reviewed step output
    reviewed_step = flow.state.steps.get(step_to_review_id) if step_to_review_id else None
    step_output = reviewed_step.outputs if reviewed_step else {}

    # Check for previous revision feedback
    revision_feedback = None
    if reviewed_step and reviewed_step.inputs.get('is_revision'):
        revision_feedback = reviewed_step.inputs.get('revision_feedback')

    # Build prompt. plan confirms are always specialized into a dedicated
    # requirement-coverage review (decompose requirements -> check every
    # requirement has a covering task), fully decoupled from the generic
    # per-step confirm; every other step keeps the generic review prompt.
    project_root = resolve_flow_project_root(flow)
    task_description = step.inputs.get("task_description", flow.task_description)
    if step_to_review_type == 'plan':
        prompt = build_plan_confirm_prompt(
            step_output=step_output,
            task_description=task_description,
            revision_feedback=revision_feedback,
            project_root=project_root,
        )
    else:
        prompt = build_llm_review_prompt(
            step_to_review_type=step_to_review_type,
            step_output=step_output,
            task_description=task_description,
            revision_feedback=revision_feedback,
            project_root=project_root,
        )

    # Call LLM
    try:
        caller = LLMCaller(
            project_root=project_root,
            flow_id=flow.flow_id,
            step_id=step.step_id,
            step_type="confirm_llm_review",
            agents=agents,
            fix_iteration=step.inputs.get("fix_iteration", 0),
        )
        response = caller.call(
            prompt=prompt,
            json_mode="two_phase",
            json_schema_hint='{"approved": true/false, "feedback": "Your detailed feedback here"}',
            required_keys=["approved"],
        )

        # Parse response using robust JSON parser that handles markdown
        # code fences, NDJSON streams, and other common LLM output formats
        response_data = parse_json_response(response, required_keys=['approved'])
        if response_data is None:
            logger.warning("LLM review returned malformed response, treating as not approved")
            approved = False
            feedback = "LLM review response could not be parsed"
        else:
            approved = response_data.get('approved', False)
            feedback = response_data.get('feedback', '')
    except Exception as e:
        logger.error(f"LLM review call failed, auto-approving to avoid blocking: {e}")
        approved = True
        feedback = f"Auto-approved due to LLM call failure: {e}"

    # NOTE: the iteration counter is intentionally NOT incremented here.
    # The single source of truth for the cross-revision review count is
    # flow.state.review_iterations, incremented once per revision by
    # _transition_to_revision. Incrementing here too would double-count.

    review_result = {
        'approved': approved,
        'feedback': feedback,
        'step_to_review_id': step_to_review_id,
        'step_to_review_type': step_to_review_type,
        'reviewer': 'llm',
    }

    if approved:
        logger.info(f"LLM reviewer approved {step_to_review_type}: {feedback[:100]}")
        return StepStatus.COMPLETED, review_result
    else:
        logger.info(f"LLM reviewer requested revision of {step_to_review_type}: {feedback[:100]}")
        return StepStatus.REVISION_NEEDED, review_result


def confirm_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Handle CONFIRM step execution.

    On first run: Creates call file and waits for human response.
    On resume: Checks for existing response and processes it.

    The handler returns different statuses based on human response:
    - COMPLETED if human approved (flow continues forward)
    - REVISION_NEEDED if human requested changes (flow goes back)
    - PAUSED if waiting for response (flow pauses)

    Args:
        step: The current CONFIRM step
        flow: The flow instance

    Returns:
        StepStatus indicating the result of confirmation
    """
    logger.info(f"Executing CONFIRM step: {step.step_id}")

    # LLM reviewer path — any reviewer other than 'human' (a specific
    # agent name, or None for the llm_caller.defaults fallback) goes
    # through synchronous LLM review with no call file and no PAUSED.
    reviewer = step.inputs.get('reviewer')
    if reviewer != 'human':
        status, review_result = _llm_review(step, flow)
        step.outputs['review_result'] = review_result
        step.outputs['revision_feedback'] = review_result.get('feedback', '')
        return status

    # Human reviewer path
    project_root = resolve_flow_project_root(flow)
    calls_dir = runtime_dir(project_root) / "calls"

    change_id = flow.change_name or flow.flow_id

    # Look for existing call file for this step/change
    call_file = None
    if calls_dir.exists():
        for f in calls_dir.glob("confirm_*.json"):
            try:
                with open(f) as cf:
                    data = json.load(cf)
                if data.get('step') == step.step_id:
                    call_file = f
                    break
            except (json.JSONDecodeError, IOError):
                continue

    # Check if we already have a response (resume case)
    if call_file:
        existing_response = _check_existing_response(call_file)
        if existing_response:
            logger.info(f"Found existing response: approved={existing_response['approved']}")

            approved = existing_response['approved']
            feedback = existing_response['feedback']
            step_to_review_id = step.inputs.get('step_to_review_id')
            step_to_review_type = step.inputs.get('step_to_review_type')

            # Store result in step outputs for state machine
            step.outputs['review_result'] = {
                'approved': approved,
                'feedback': feedback,
                'step_to_review_id': step_to_review_id,
                'step_to_review_type': step_to_review_type,
            }

            if approved:
                # Approved - complete the confirm step
                step.outputs['revision_feedback'] = feedback
                return StepStatus.COMPLETED
            else:
                # Changes requested - mark for revision
                step.outputs['revision_feedback'] = feedback
                return StepStatus.REVISION_NEEDED

    # No existing response - create call file and pause for interactive handling
    if not call_file:
        call_file = _create_call_file(step, flow, project_root)

    # Store the call file path so the run loop can find it
    step.outputs['call_file'] = str(call_file)

    # Return PAUSED so the run loop can prompt the user interactively
    logger.info("Confirm step paused, waiting for interactive response")
    return StepStatus.PAUSED
