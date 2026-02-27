"""Confirm step handler for human review."""

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from ..models import FlowInstance, Step, StepStatus, StepType

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
    calls_dir = project_root / "se3" / "calls"
    calls_dir.mkdir(parents=True, exist_ok=True)

    # Use timestamp and step name for unique filename
    timestamp = int(time.time())
    change_id = flow.change_name or flow.flow_id
    call_file = calls_dir / f"confirm_{step.step_id}_{timestamp}.json"

    # Get the step being reviewed
    step_to_review_id = step.inputs.get('step_to_review_id')
    step_to_review_type = step.inputs.get('step_to_review_type', 'unknown')

    call_data = {
        'step': step.step_id,
        'change_id': change_id,
        'step_to_review_type': step_to_review_type,
        'step_to_review_id': step_to_review_id,
        'timestamp': timestamp,
        'type': 'confirm'
    }

    with open(call_file, 'w') as f:
        json.dump(call_data, f, indent=2)

    logger.info(f"Created confirmation call file: {call_file}")
    return call_file


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

    project_root = flow.change_path.parent if flow.change_path else Path.cwd()
    calls_dir = project_root / "se3" / "calls"

    change_id = flow.change_name or flow.flow_id

    # Look for existing call file for this step/change
    call_file = None
    if calls_dir.exists():
        for f in calls_dir.glob("confirm_*.json"):
            try:
                with open(f) as cf:
                    data = json.load(cf)
                if data.get('change_id') == change_id or data.get('step') == step.step_id:
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

    # No existing response - create call file and wait
    if not call_file:
        call_file = _create_call_file(step, flow, project_root)

    # Wait for human response
    try:
        response = _wait_for_human_response(call_file, timeout=None)

        approved = response['approved']
        feedback = response['feedback']
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
            # Return COMPLETED for approval so state machine transitions forward
            step.outputs['revision_feedback'] = feedback
            return StepStatus.COMPLETED
        else:
            # Return REVISION_NEEDED to trigger backward transition
            step.outputs['revision_feedback'] = feedback
            return StepStatus.REVISION_NEEDED

    except TimeoutError:
        logger.error("Timeout waiting for human response")
        step.error_message = "Timeout waiting for human confirmation"
        return StepStatus.FAILED
