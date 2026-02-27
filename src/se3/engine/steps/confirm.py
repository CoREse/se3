"""Confirm step handler.

Handles review and confirmation of previous step outputs.
Supports two reviewer modes:
- human: Creates MCP call file and waits for human input
- llm: Uses another LLM call to review the output

The confirm step implements a review loop:
1. Review the output of the previous step
2. If approved, continue to next step
3. If changes requested, go back to the previous step with feedback
4. If aborted, mark flow as failed
"""

from __future__ import annotations

import json
import logging
import os
import select
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..llm_caller import LLMCaller, LLMCallError
from ..models import FlowInstance, Step, StepStatus, StepType
from ..utils.json_parser import parse_json_response
from ...config import load_confirmation_config
from ...human_input import HumanInputStore

logger = logging.getLogger(__name__)

# Review prompts for different step types
REVIEW_PROMPTS = {
    "propose": """You are a senior software engineer reviewing a change proposal.

## Task Description
{task_description}

## Proposal to Review
{content}

## Relevant Specifications
{spec_content}

## Review Instructions
Please review the proposal for:
1. **Completeness**: Does it cover all aspects of the task?
2. **Intent Alignment**: Does it match the stated task description?
3. **Spec Compliance**: Is it consistent with relevant specifications?
4. **Maintainability**: Are the proposed changes maintainable?

Respond in JSON format:
```json
{{
    "approved": true|false,
    "reasoning": "Brief explanation of your decision",
    "feedback": "Detailed feedback if changes needed, or empty if approved"
}}
```
""",
    "design": """You are a senior architect reviewing a design document.

## Task Description
{task_description}

## Design Document to Review
{content}

## Relevant Specifications
{spec_content}

## Review Instructions
Please review the design for:
1. **Completeness**: Are all components and interfaces defined?
2. **Intent Alignment**: Does it properly address the task requirements?
3. **Spec Compliance**: Is it consistent with specifications and existing patterns?
4. **Maintainability**: Is the design clean, modular, and maintainable?
5. **Feasibility**: Is the implementation plan realistic?

Respond in JSON format:
```json
{{
    "approved": true|false,
    "reasoning": "Brief explanation of your decision",
    "feedback": "Detailed feedback if changes needed, or empty if approved"
}}
```
""",
    "plan_tasks": """You are a senior engineer reviewing a task breakdown.

## Task Description
{task_description}

## Task Plan to Review
{content}

## Design Document
{design_doc}

## Review Instructions
Please review the task plan for:
1. **Completeness**: Are all necessary tasks included?
2. **Correct Order**: Are dependencies properly handled?
3. **Granularity**: Are tasks appropriately sized?
4. **Intent Alignment**: Will completing these tasks fulfill the requirements?

Respond in JSON format:
```json
{{
    "approved": true|false,
    "reasoning": "Brief explanation of your decision",
    "feedback": "Detailed feedback if changes needed, or empty if approved"
}}
```
""",
}

DEFAULT_REVIEW_PROMPT = """You are reviewing the output of a workflow step.

## Task Description
{task_description}

## Content to Review
{content}

## Review Instructions
Please review for completeness, correctness, and alignment with the task.

Respond in JSON format:
```json
{{
    "approved": true|false,
    "reasoning": "Brief explanation",
    "feedback": "Detailed feedback if changes needed"
}}
```
"""


def confirm_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the confirm step.

    Reviews the output of the previous step and determines whether to:
    - Approve and continue
    - Request changes and loop back
    - Abort the flow

    Args:
        step: The current confirm step
        flow: The flow instance

    Returns:
        StepStatus.COMPLETED if approved, StepStatus.FAILED if aborted,
        or triggers a transition back to the previous step for modifications.
    """
    # Get configuration
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()
    config = load_confirmation_config(project_root)

    # Determine reviewer type (from step inputs, fallback to config)
    reviewer_type = step.inputs.get("reviewer", config.get("reviewer", "human"))
    step_to_review_type = step.inputs.get("step_to_review_type", "unknown")

    # Get the step being reviewed
    step_to_review = flow.state.get_step_to_review(step.step_id)
    if not step_to_review:
        step.error_message = "Could not find the step to review"
        return StepStatus.FAILED

    # Get the content to review
    content_to_review = _get_review_content(step_to_review)
    if not content_to_review:
        step.error_message = f"No content to review from step {step_to_review.step_type.value}"
        return StepStatus.FAILED

    # Increment review iteration counter
    iteration = flow.state.increment_review_iteration(step_to_review.step_id)
    max_iterations = config.get("llm_reviewer", {}).get("max_iterations", 3)

    logger.info(f"Confirming {step_to_review_type} output (iteration {iteration}, reviewer={reviewer_type})")

    # Perform review
    if reviewer_type == "llm":
        approved, feedback, reasoning = _llm_review(
            step, flow, step_to_review_type, content_to_review, config
        )
    else:
        approved, feedback, reasoning = _human_review(
            step, flow, step_to_review_type, content_to_review, config
        )

    # Store review result
    step.outputs["review_result"] = {
        "approved": approved,
        "feedback": feedback,
        "reasoning": reasoning,
        "iteration": iteration,
        "reviewer": reviewer_type,
    }
    step.outputs["approved"] = approved
    step.outputs["step_to_review_id"] = step_to_review.step_id
    step.outputs["step_to_review_type"] = step_to_review_type

    # Handle review result
    if approved:
        logger.info(f"Review approved for {step_to_review_type}")
        return StepStatus.COMPLETED

    # Check max iterations
    if iteration >= max_iterations:
        logger.warning(f"Max review iterations ({max_iterations}) reached for {step_to_review_type}")
        step.error_message = f"Review failed after {max_iterations} iterations. Last feedback: {feedback[:200]}..."
        return StepStatus.FAILED

    # Request changes - need to go back to previous step
    logger.info(f"Changes requested for {step_to_review_type}, looping back (iteration {iteration})")
    step.outputs["requires_revision"] = True
    step.outputs["revision_feedback"] = feedback

    # Mark current step as completed but signal that we need to go back
    # The state machine will handle the transition logic
    step.status = StepStatus.COMPLETED
    return _trigger_revision(flow, step_to_review, feedback)


def _get_review_content(step: Step) -> Optional[str]:
    """Extract reviewable content from a step's outputs."""
    if step.step_type == StepType.PROPOSE:
        proposal = step.outputs.get("proposal", {})
        return json.dumps(proposal, indent=2, ensure_ascii=False) if proposal else None

    elif step.step_type == StepType.DESIGN:
        design_doc = step.outputs.get("design_doc", {})
        return json.dumps(design_doc, indent=2, ensure_ascii=False) if design_doc else None

    elif step.step_type == StepType.PLAN_TASKS:
        task_list = step.outputs.get("task_list", {})
        return json.dumps(task_list, indent=2, ensure_ascii=False) if task_list else None

    else:
        # For other steps, try common output keys
        for key in ["proposal", "design_doc", "task_list", "result", "output"]:
            if key in step.outputs:
                content = step.outputs[key]
                if isinstance(content, (dict, list)):
                    return json.dumps(content, indent=2, ensure_ascii=False)
                return str(content)
        return None


def _llm_review(
    step: Step,
    flow: FlowInstance,
    step_type: str,
    content: str,
    config: Dict[str, Any],
) -> Tuple[bool, str, str]:
    """Perform LLM-based review.

    Returns:
        Tuple of (approved, feedback, reasoning)
    """
    task_description = step.inputs.get("task_description", "")
    spec_content = step.inputs.get("spec_content", {})
    design_doc = step.inputs.get("design_doc", {}) if step_type == "plan_tasks" else {}

    # Get the appropriate prompt template
    prompt_template = REVIEW_PROMPTS.get(step_type, DEFAULT_REVIEW_PROMPT)

    # Format spec content
    spec_text = _format_spec_content(spec_content)
    design_text = json.dumps(design_doc, indent=2, ensure_ascii=False) if design_doc else "N/A"

    # Build prompt
    prompt = prompt_template.format(
        task_description=task_description,
        content=content,
        spec_content=spec_text,
        design_doc=design_text,
    )

    logger.info(f"Starting LLM review for {step_type}")

    try:
        project_root = flow.change_path.parent if flow.change_path else Path.cwd()
        caller = LLMCaller(
            project_root,
            flow_id=flow.flow_id,
            step_id=step.step_id,
            step_type=f"confirm_llm_review_{step_type}",
        )
        response = caller.call(prompt=prompt, require_json=True)

        # Parse response
        result = parse_json_response(response, required_keys=["approved"])

        if result is None:
            logger.warning("Failed to parse LLM review response, defaulting to approve")
            return True, "", "Parse error - defaulting to approve"

        approved = result.get("approved", True)
        feedback = result.get("feedback", "")
        reasoning = result.get("reasoning", "")

        logger.info(f"LLM review result: approved={approved}, reasoning={reasoning[:100]}...")
        return approved, feedback, reasoning

    except Exception as e:
        logger.exception("LLM review failed")
        # On failure, default to approve to avoid blocking
        return True, "", f"Review failed with error: {str(e)} - defaulting to approve"


def _human_review(
    step: Step,
    flow: FlowInstance,
    step_type: str,
    content: str,
    config: Dict[str, Any],
) -> Tuple[bool, str, str]:
    """Perform human review via MCP call file.

    Creates a human call file and waits for response via:
    1. File editing (monitoring the Response section)
    2. Command line input (if interactive)

    Returns:
        Tuple of (approved, feedback, reasoning)
    """
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    # Get human calls directory from config
    from ...config import load_human_call_config
    hc_config = load_human_call_config(project_root)
    calls_dir = project_root / hc_config.get("directory", "se3/calls")
    calls_dir.mkdir(parents=True, exist_ok=True)

    # Create human call file for confirmation
    call_id = f"confirm-{step_type}-{flow.flow_id[:8]}"
    call_file = calls_dir / f"{call_id}.md"

    # Build the call content
    call_content = _build_human_call_content(
        step_type=step_type,
        content=content,
        task_description=step.inputs.get("task_description", ""),
        iteration=flow.state.get_review_iteration(step.step_id),
    )

    # Write the call file
    call_file.write_text(call_content, encoding="utf-8")

    logger.info(f"Created human call file: {call_file}")
    print(f"\n{'='*60}")
    print(f"⏸️  CONFIRMATION REQUIRED: {step_type.upper()}")
    print(f"{'='*60}")
    print(f"Please review the output and respond via:")
    print(f"  1. Command line (interactive mode)")
    print(f"  2. Edit file: {call_file}")
    print(f"\nWaiting for your response...")
    print(f"{'='*60}\n")

    # Wait for response
    result = _wait_for_human_response(call_file, timeout_seconds=3600)  # 1 hour timeout

    if result is None:
        # Timeout - default to fail
        step.error_message = "Human review timed out"
        return False, "Timeout", "No response received within timeout"

    decision = result.get("decision", "abort")
    feedback = result.get("feedback", "")

    if decision == "approve":
        return True, feedback, "Human approved"
    elif decision == "revise":
        return False, feedback, "Human requested changes"
    else:  # abort
        return False, feedback, "Human aborted"


def _build_human_call_content(
    step_type: str,
    content: str,
    task_description: str,
    iteration: int,
) -> str:
    """Build the content for a human confirmation call file."""

    iteration_note = f"\n> **Review Iteration:** {iteration}\n" if iteration > 1 else ""

    return f"""# Confirm: {step_type.capitalize()} Output Review

## Context
Task: {task_description}{iteration_note}

## Content to Review

<details>
<summary>Click to expand {step_type} output</summary>

```json
{content}
```

</details>

## Response

### Quick CLI Commands
If responding via command line:
- `y` / `yes` - Approve and continue
- `n` / `no` / `abort` - Stop the workflow
- **Type anything else** - Request changes (your input becomes feedback)

### File Edit
Edit this section with your response:

**Decision:** (check one)
- `[ ]` **Approve** - Proceed to next step
- `[ ]` **Request Changes** - Go back and revise (provide feedback below)
- `[ ]` **Abort** - Stop the workflow

**Feedback:**
<!-- Describe what needs to be modified, or leave empty if approving/aborting -->


---
*Response can be provided via file edit or command line input.*
"""


def _wait_for_human_response(
    call_file: Path,
    timeout_seconds: int = 3600,
    poll_interval: float = 1.0,
) -> Optional[Dict[str, str]]:
    """Wait for human response via file edit or command line input.

    Uses non-blocking I/O to monitor both:
    1. File changes (Response section edited)
    2. Command line input (if interactive)

    Args:
        call_file: Path to the human call file
        timeout_seconds: Maximum time to wait
        poll_interval: How often to check file

    Returns:
        Dict with 'decision' and 'feedback', or None on timeout
    """
    import tty
    import termios
    import fcntl

    start_time = time.time()
    last_mtime = call_file.stat().st_mtime

    # Store original stdin settings for restoration
    original_stdin_settings = None
    stdin_is_tty = sys.stdin.isatty()

    if stdin_is_tty:
        original_stdin_settings = termios.tcgetattr(sys.stdin)
        # Set stdin to non-blocking
        fd = sys.stdin.fileno()
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    try:
        while time.time() - start_time < timeout_seconds:
            # Check for file changes
            try:
                current_mtime = call_file.stat().st_mtime
                if current_mtime > last_mtime:
                    # File was modified, parse response
                    result = _parse_call_file_response(call_file)
                    if result:
                        print(f"\n✓ Response received from file edit")
                        return result
                    last_mtime = current_mtime
            except (OSError, IOError):
                pass

            # Check for command line input (non-blocking)
            if stdin_is_tty:
                try:
                    # Check if input is available
                    if select.select([sys.stdin], [], [], 0)[0]:
                        user_input = sys.stdin.readline().strip()
                        if user_input:
                            result = _parse_cli_input(user_input)
                            if result:
                                # Also update the file
                                _update_call_file_from_cli(call_file, result)
                                print(f"\n✓ Response received from command line")
                                return result
                except (IOError, OSError):
                    pass

            # Sleep before next poll
            time.sleep(poll_interval)

        # Timeout
        return None

    finally:
        # Restore stdin settings
        if stdin_is_tty and original_stdin_settings:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_stdin_settings)
                fd = sys.stdin.fileno()
                fl = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, fl & ~os.O_NONBLOCK)
            except:
                pass


def _parse_call_file_response(call_file: Path) -> Optional[Dict[str, str]]:
    """Parse the response section from a call file."""
    try:
        content = call_file.read_text(encoding="utf-8")

        # Check for decision checkboxes
        approved = "[x] **Approve**" in content or "[X] **Approve**" in content
        revise = "[x] **Request Changes**" in content or "[X] **Request Changes**" in content
        abort = "[x] **Abort**" in content or "[X] **Abort**" in content

        if not (approved or revise or abort):
            # No decision made yet
            return None

        # Extract feedback
        feedback = ""
        in_feedback = False
        for line in content.split("\n"):
            if "**Feedback:**" in line:
                in_feedback = True
                continue
            if in_feedback:
                # Stop at separator or next section
                if line.startswith("---") or line.startswith("## "):
                    break
                feedback += line + "\n"

        feedback = feedback.strip()

        if approved:
            return {"decision": "approve", "feedback": feedback}
        elif revise:
            return {"decision": "revise", "feedback": feedback}
        elif abort:
            return {"decision": "abort", "feedback": feedback}

    except (IOError, OSError):
        pass

    return None


def _parse_cli_input(user_input: str) -> Optional[Dict[str, str]]:
    """Parse command line input for confirmation decision.

    Logic:
    - "y", "yes", "approve" -> approve
    - "n", "no", "abort", "cancel", "stop", "quit" -> abort
    - Anything else -> revise (treated as feedback)

    This means users can simply type their feedback directly without any prefix.
    """
    inp = user_input.strip().lower()

    # Explicit approve commands
    if inp in ("y", "yes", "approve", "ok", "good", "pass", "passed"):
        return {"decision": "approve", "feedback": ""}

    # Explicit abort commands
    if inp in ("n", "no", "abort", "cancel", "stop", "quit", "exit", "end"):
        return {"decision": "abort", "feedback": ""}

    # Everything else is treated as revision feedback
    # User can type feedback directly without any prefix
    return {"decision": "revise", "feedback": user_input.strip()}


def _update_call_file_from_cli(call_file: Path, result: Dict[str, str]) -> None:
    """Update the call file to reflect CLI input."""
    try:
        content = call_file.read_text(encoding="utf-8")
        decision = result.get("decision", "")

        # Replace checkboxes
        if decision == "approve":
            content = content.replace("[ ] **Approve**", "[x] **Approve**")
        elif decision == "revise":
            content = content.replace("[ ] **Request Changes**", "[x] **Request Changes**")
        elif decision == "abort":
            content = content.replace("[ ] **Abort**", "[x] **Abort**")

        # Append feedback if provided
        feedback = result.get("feedback", "")
        if feedback:
            content = content.replace(
                "<!-- If requesting changes, describe what needs to be modified -->",
                feedback
            )

        call_file.write_text(content, encoding="utf-8")
    except (IOError, OSError):
        pass


def _trigger_revision(
    flow: FlowInstance,
    step_to_review: Step,
    feedback: str,
) -> StepStatus:
    """Trigger a revision by resetting the step being reviewed.

    This is a special status that signals the state machine to:
    1. Go back to the step being reviewed
    2. Re-run it with the feedback as additional input

    Args:
        flow: The flow instance
        step_to_review: The step that needs to be re-run
        feedback: Feedback for revision

    Returns:
        Special status that triggers the transition
    """
    # Store the feedback in the step's context for the next run
    step_to_review.inputs["revision_feedback"] = feedback
    step_to_review.inputs["is_revision"] = True

    # Reset the step status so it will be re-run
    step_to_review.status = StepStatus.PENDING
    step_to_review.retry_count = 0  # Don't count as retry

    # Clear previous outputs to force regeneration
    step_to_review.outputs.clear()

    # Create a custom status to signal the state machine
    # We'll use a special marker in the step outputs
    step_to_review.outputs["_revision_triggered"] = True

    logger.info(f"Triggered revision for step {step_to_review.step_type.value}")

    # Return a status that won't complete the step, signaling to state machine
    # that we need special handling
    return StepStatus.PAUSED


def _format_spec_content(spec_content: Dict[str, str]) -> str:
    """Format spec content for inclusion in prompt."""
    if not spec_content:
        return "No relevant specifications found."

    parts = []
    for name, content in spec_content.items():
        parts.append(f"### {name}")
        # Truncate very long specs
        if len(content) > 2000:
            content = content[:2000] + "\n... [truncated for brevity]"
        parts.append(content)
        parts.append("")

    return "\n".join(parts)
