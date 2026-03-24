"""SE3 Run command — The unified entry point for SE3 3.0 flow engine.

Replaces start/work/done with a state machine-driven workflow that:
- Creates new flows or resumes interrupted ones
- Runs in single mode (one task) or loop mode (continuous)
- Handles all step types programmatically

Usage:
    se3 run "Implement feature X"              # New flow
    se3 run --resume                           # Resume interrupted flow
    se3 run --loop                             # Loop mode (find next task automatically)
    se3 run "Fix bug" --type=bugfix            # Specify task type
"""

from __future__ import annotations

import json
import logging
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import typer

# Add engine to path if needed
try:
    from ..engine.models import FlowInstance, FlowStatus, StepStatus, StepType
    from ..engine.persistence import PersistenceManager
    from ..engine.state_machine import StateMachine
    from ..engine.context_builder import ContextBuilder
    from ..engine.steps import STEP_HANDLERS
    from ..engine.llm_caller import set_extra_prompt
    from ..engine.output import (
        display_analysis,
        display_design,
        display_error,
        display_proposal,
        display_step_result,
        display_success,
        format_output,
        render_full,
    )
    from ..cli import _read_multiline_input
except ImportError:
    # Direct import for development
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from engine.models import FlowInstance, FlowStatus, StepStatus, StepType
    from engine.persistence import PersistenceManager
    from engine.state_machine import StateMachine
    from engine.context_builder import ContextBuilder
    from engine.steps import STEP_HANDLERS
    from engine.llm_caller import set_extra_prompt
    from engine.output import (
        display_analysis,
        display_design,
        display_error,
        display_proposal,
        display_step_result,
        display_success,
        format_output,
        render_full,
    )
    from cli import _read_multiline_input


app = typer.Typer()
logger = logging.getLogger(__name__)

SE3_DIR = "se3"
STATE_FILE = "state/engine.json"

# Global flag set by SIGINT handler to detect interrupt requests
_interrupt_requested = False


def _sigint_handler(signum: int, frame: Any) -> None:
    """Handle SIGINT by setting a flag and re-raising KeyboardInterrupt.

    This ensures Ctrl-C is never lost, even during blocking subprocess calls.
    """
    global _interrupt_requested
    _interrupt_requested = True
    raise KeyboardInterrupt


def get_project_root() -> Path:
    """Find project root by looking for .git directory or se3.yaml."""
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        # Check for .git directory
        if (parent / ".git").exists():
            return parent
        # Check for se3.yaml (project config file)
        if (parent / "se3.yaml").exists() or (parent / "se3.config.yaml").exists():
            return parent
    return cwd


def _check_confirm_response(flow: FlowInstance, current_step: Any, project_root: Path) -> Optional[StepStatus]:
    """Check for existing human response when resuming a CONFIRM step.

    This prevents duplicate call files and enables immediate continuation
    if the human has already responded.

    Args:
        flow: Current flow instance
        current_step: The CONFIRM step being resumed
        project_root: Project root directory

    Returns:
        StepStatus if response found and processed, None otherwise
    """
    calls_dir = project_root / "se3" / "calls"
    if not calls_dir.exists():
        return None

    for call_file in calls_dir.glob("confirm_*.json"):
        try:
            with open(call_file) as f:
                data = json.load(f)

            # Match by step_id only — change_id is flow-level and would
            # cause different confirm steps to share the same response
            if data.get('step') == current_step.step_id:
                # Check for response file
                response_path = call_file.parent / f"{call_file.stem}.response"
                if response_path.exists():
                    try:
                        with open(response_path) as f:
                            response_data = json.load(f)

                        approved = response_data.get('approved', False)
                        feedback = response_data.get('feedback')

                        # Store result in step outputs for state machine
                        current_step.outputs['review_result'] = {
                            'approved': approved,
                            'feedback': feedback,
                            'step_to_review_id': current_step.inputs.get('step_to_review_id'),
                            'step_to_review_type': current_step.inputs.get('step_to_review_type'),
                        }

                        if approved:
                            current_step.outputs['revision_feedback'] = feedback
                            return StepStatus.COMPLETED
                        else:
                            current_step.outputs['revision_feedback'] = feedback
                            return StepStatus.REVISION_NEEDED

                    except (json.JSONDecodeError, IOError):
                        logger.warning(f"Failed to parse response file: {response_path}")
                        continue
        except (json.JSONDecodeError, IOError):
            continue

    return None


def _handle_confirm_pause(
    flow: FlowInstance,
    current_step: Any,
    persistence: Any,
    project_root: Path,
    prompt_history: Any = None,
) -> Optional[bool]:
    """Handle interactive confirmation when CONFIRM step is paused.

    Displays the reviewed step's content and prompts the user to approve or
    request changes. Writes the response file so the confirm handler can
    process it on re-run.

    Returns:
        True if response was written, None if user chose to exit.
    """
    step_to_review_id = current_step.inputs.get("step_to_review_id")
    step_to_review_type = current_step.inputs.get("step_to_review_type", "unknown")

    # The reviewed step's output was already displayed by _display_step_output
    # in the previous iteration, so just prompt directly.
    options = ["Approve and continue", "Request changes", "Exit (pause flow)"]
    try:
        choice = prompt_user_choice(
            f"Review {step_to_review_type} output above:", options
        )
    except (KeyboardInterrupt, EOFError):
        persistence.save_flow(flow)
        return None

    if choice == 2:
        # Exit
        persistence.save_flow(flow)
        return None

    approved = choice == 0
    feedback = None

    if not approved:
        # Get feedback from user
        feedback = _read_multiline_input(
            prompt_title="Feedback",
            prompt_message="Describe the changes you'd like (Ctrl+D or Esc+Enter to finish, Ctrl+C to cancel):",
            history=prompt_history,
        )
        if feedback is None:
            persistence.save_flow(flow)
            return None

    # Write the response file
    call_file_path = current_step.outputs.get("call_file")
    if call_file_path:
        call_file = Path(call_file_path)
        response_path = call_file.parent / f"{call_file.stem}.response"
        response_data = {
            "approved": approved,
            "feedback": feedback,
            "step_to_review_id": step_to_review_id,
            "step_to_review_type": step_to_review_type,
        }
        with open(response_path, "w") as f:
            json.dump(response_data, f, indent=2, ensure_ascii=False)

    status_text = "Approved" if approved else f"Changes requested: {feedback}"
    render_full(status_text, title="Confirmation Result")

    return True


def find_existing_flows(project_root: Path) -> List[Dict[str, Any]]:
    """Find all existing flow state files."""
    flows = []
    se3_dir = project_root / SE3_DIR
    state_file = se3_dir / "state" / "engine.json"

    if not state_file.exists():
        return flows

    try:
        with open(state_file) as f:
            data = json.load(f)
            state_data = data.get("state", {})
            flows.append({
                "id": data.get("flow_id", "unknown"),
                "status": data.get("status", "unknown"),
                "description": data.get("task_description", "No description"),
                "current_step": state_data.get("current_step_id"),
                "file": state_file.name,
            })
    except (json.JSONDecodeError, IOError):
        pass

    return flows


def prompt_user_choice(message: str, options: List[str]) -> int:
    """Prompt user to select an option."""
    print(f"\n{message}")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")

    while True:
        try:
            choice = input("\nSelect (number): ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return idx
            print(f"Please enter a number between 1 and {len(options)}")
        except ValueError:
            print("Please enter a valid number")
        except EOFError:
            # Handle non-interactive mode - default to last option (typically Abort)
            print(f"Non-interactive mode detected, selecting option {len(options)} ({options[-1]})")
            return len(options) - 1


def handle_resume_interactive(project_root: Path) -> Optional[str]:
    """Handle interactive resume flow.

    Returns:
        Flow ID to resume, or None if user chooses new flow.
    """
    flows = find_existing_flows(project_root)

    if not flows:
        render_full("No existing flows found. Starting new flow.", title="Resume")
        return None

    # Filter to active (non-terminal) flows
    terminal_statuses = {FlowStatus.COMPLETED.value, FlowStatus.FAILED.value}
    active_flows = [f for f in flows if f["status"] not in terminal_statuses]

    if not active_flows:
        render_full("No in-progress flows found.", title="Resume")
        if flows:
            render_full(f"Found {len(flows)} completed/failed flows.", title="Info")
        return None

    if len(active_flows) == 1:
        flow = active_flows[0]
        content = [
            "Found interrupted flow:",
            "",
            f"  ID: {flow['id']}",
            f"  Description: {flow['description']}",
            f"  Current step: {flow['current_step']}",
        ]
        render_full("\n".join(content), title="Resume Flow")

        options = ["Resume this flow", "Start new flow"]
        choice = prompt_user_choice("What would you like to do?", options)

        if choice == 0:
            return flow['id']
        return None

    # Multiple active flows
    content = [f"Found {len(active_flows)} interrupted flows:", ""]
    options = []
    for flow in active_flows:
        options.append(f"{flow['description']} (step: {flow['current_step']})")
    options.append("Start new flow")

    for i, opt in enumerate(options[:-1], 1):
        content.append(f"  {i}. {opt}")
    content.append(f"  {len(options)}. Start new flow")

    render_full("\n".join(content), title="Resume Flow")
    choice = prompt_user_choice("Which flow to resume?", options)

    if choice < len(active_flows):
        return active_flows[choice]["id"]
    return None


def _handle_step_interrupt(flow: FlowInstance, current_step: Any, persistence: PersistenceManager, prompt_history: Any = None) -> Optional[StepStatus]:
    """Handle KeyboardInterrupt during step execution.

    Returns:
        StepStatus to continue, or None to exit
    """
    user_input = _read_multiline_input(
        prompt_title="Additional Instruction",
        prompt_message="Enter additional instruction (Ctrl+D or Esc+Enter to finish, Ctrl+C to cancel, empty to retry as-is):",
        history=prompt_history,
    )
    if user_input is None:
        # Cancelled (Ctrl+C): save and exit
        persistence.save_flow(flow)
        render_full(
            "Interrupted by user. Flow state saved.\n"
            "Resume with: se3 run --resume",
            title="Exit"
        )
        return None
    if user_input:
        set_extra_prompt(user_input)
        render_full("Extra prompt set, retrying step...", title="Info")
    else:
        render_full("Retrying step as-is...", title="Info")
    # Reset step to PENDING so it re-runs
    current_step.status = StepStatus.PENDING
    persistence.save_flow(flow)
    # Return a special marker to indicate retry
    return StepStatus.PENDING


def _handle_discovery_pause(flow: FlowInstance, current_step: Any, persistence: PersistenceManager, prompt_history: Any = None) -> Optional[str]:
    """Handle discovery step pause - get user response.

    Args:
        flow: Current flow instance
        current_step: The discovery step
        persistence: Persistence manager

    Returns:
        User response string, "__RESUME__" if resuming, or None to exit
    """
    render_full(
        "Discovery mode is exploring your requirements.\n"
        "Please respond to the questions above to help clarify what you want to build.",
        title="Discovery Pause"
    )

    user_input = _read_multiline_input(
        prompt_title="Discovery Response",
        prompt_message="Enter your response (Ctrl+D or Esc+Enter to finish, Ctrl+C to cancel):",
        history=prompt_history,
    )

    if user_input is None:
        # User cancelled
        persistence.save_flow(flow)
        render_full(
            "Discovery paused. Flow state saved.\n"
            "Resume with: se3 run --resume",
            title="Paused"
        )
        return None

    if not user_input:
        # Empty input - check if we're resuming
        if current_step.inputs.get("resumed"):
            return "__RESUME__"
        # Otherwise ask again
        render_full("Please provide a response or press Ctrl+C to exit.", title="Input Required")
        return _handle_discovery_pause(flow, current_step, persistence, prompt_history)

    # Parse the response to check if user is confirming
    from ..engine.steps.discovery import parse_user_response
    parsed = parse_user_response(user_input)

    # If user confirmed a synthesis, mark it
    if parsed["confirmed"] and current_step.inputs.get("discovery_state", {}).get("mode") == "synthesis":
        current_step.outputs["user_confirmed"] = True

    return user_input


def _display_step_output(current_step: Any) -> None:
    """Display step output using full-content rendering.

    Inputs are omitted — they are internal context passed between steps and
    not useful for the user.  Large output fields (spec content, test results,
    etc.) are summarised rather than dumped verbatim.

    Args:
        current_step: The current step being executed
    """
    step_header = f"Step: {current_step.step_type.value}"
    step_info = f"Status: {current_step.status.value}"

    lines = [f"[bold]{step_info}[/bold]", ""]

    # --- Outputs only (inputs are internal plumbing) ---
    # Keys whose raw content is too large to display directly
    DEFERRED_KEYS = {"proposal", "proposal_data", "design", "design_doc",
                     "design_document", "analysis", "analysis_result"}
    LARGE_KEYS = {"spec_content", "spec_summary", "test_results",
                  "project_summary"}
    HIDDEN_KEYS = {"result", "call_file"}

    if current_step.outputs:
        lines.append("[bold green]Outputs:[/bold green]")
        for key, value in current_step.outputs.items():
            if key in HIDDEN_KEYS:
                continue

            if key in DEFERRED_KEYS and isinstance(value, dict):
                label = key.replace("_", " ").title()
                lines.append(f"\n  [bold]{key}:[/bold] (see {label} display below)")
            elif key in LARGE_KEYS:
                # Show a short summary instead of the full blob
                if isinstance(value, str):
                    preview = value[:120].replace("\n", " ")
                    lines.append(f"  [bold]{key}:[/bold] {preview}... ({len(value)} chars)")
                elif isinstance(value, dict):
                    lines.append(f"  [bold]{key}:[/bold] ({len(value)} entries)")
                else:
                    formatted_value = format_output(value)
                    lines.append(f"  [bold]{key}:[/bold] {formatted_value}")
            else:
                formatted_value = format_output(value)
                lines.append(f"  [bold]{key}:[/bold] {formatted_value}")
        lines.append("")

    if current_step.error_message:
        lines.append(f"[bold red]Error:[/bold red] {current_step.error_message}")

    content = "\n".join(lines)
    render_full(content, title=step_header)

    # Now display specific structured outputs with dedicated renderers
    outputs = current_step.outputs or {}

    # Display proposal if present
    for key in ("proposal", "proposal_data"):
        if key in outputs and isinstance(outputs[key], dict):
            display_proposal(outputs[key])
            break

    # Display design if present
    for key in ("design", "design_doc", "design_document"):
        if key in outputs and isinstance(outputs[key], dict):
            display_design(outputs[key])
            break

    # Display analysis if present
    for key in ("analysis", "analysis_result", "result"):
        if key in outputs and isinstance(outputs[key], dict):
            # Check if it looks like an analysis result
            value = outputs[key]
            if any(k in value for k in ("summary", "findings", "insights", "recommendations")):
                display_analysis(value)
                break


def _should_show_type(current_step_type: str, flow: FlowInstance) -> bool:
    """Check if task type should be displayed for the current step.

    Type should not be shown before analyze completes (when still pending).

    Args:
        current_step_type: The current step type being executed
        flow: The flow instance

    Returns:
        True if type should be shown, False otherwise
    """
    # Don't show type during analyze step or when type is still pending
    if current_step_type == StepType.ANALYZE.value:
        return False

    # Check if type is still pending
    if flow.state.is_type_pending():
        return False

    return True


def _get_display_task_type(flow: FlowInstance) -> Optional[str]:
    """Get the task type to display, or None if pending.

    Args:
        flow: The flow instance

    Returns:
        Type string for display, or None if pending
    """
    # Check for resolved type first
    resolved = flow.state.context.get("resolved_type")
    if resolved:
        return resolved

    # Check for explicit type
    explicit = flow.state.context.get("explicit_type")
    if explicit:
        return explicit

    # Check flow's task_type
    if flow.task_type:
        return flow.task_type

    # Still pending
    return None


def run_flow(
    project_root: Path,
    flow_id: Optional[str] = None,
    task_description: Optional[str] = None,
    task_type: str = "pending",
    change_name: Optional[str] = None,
    is_loop_mode: bool = False,
    prompt_history: Any = None,
) -> int:
    """Run a flow to completion.

    Args:
        project_root: Project root directory
        flow_id: Flow ID to resume (None for new flow)
        task_description: Task description for new flow
        task_type: Type of task (feature, bugfix, etc., or 'pending' to auto-detect)
        change_name: Optional change name
        is_loop_mode: Whether to run in loop mode

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Initialize components before signal handler (for clean state on early exit)
    persistence = PersistenceManager(project_root)
    state_machine = StateMachine(project_root, persistence)

    # Register SIGINT handler for reliable Ctrl-C handling
    global _interrupt_requested
    _interrupt_requested = False
    old_sigint_handler = signal.signal(signal.SIGINT, _sigint_handler)

    try:
        return _run_flow_impl(
            project_root, flow_id, task_description, task_type, change_name,
            is_loop_mode, persistence, state_machine, prompt_history
        )
    finally:
        # Restore original signal handler
        signal.signal(signal.SIGINT, old_sigint_handler)


def _run_flow_impl(
    project_root: Path,
    flow_id: Optional[str],
    task_description: Optional[str],
    task_type: str,
    change_name: Optional[str],
    is_loop_mode: bool,
    persistence: PersistenceManager,
    state_machine: StateMachine,
    prompt_history: Any = None,
) -> int:
    """Internal implementation of flow execution."""
    # Register all step handlers
    for step_type, handler in STEP_HANDLERS.items():
        state_machine.register_handler(step_type, handler)

    # Load or create flow
    if flow_id:
        flow = persistence.load_flow()
        if not flow or flow.flow_id != flow_id:
            display_error(f"Flow '{flow_id}' not found")
            return 1

        # Detect and handle resume of a RUNNING step
        current_step = flow.state.get_current_step()
        if current_step and current_step.status == StepStatus.RUNNING:
            # Step was interrupted - prepare for resumption
            current_step.status = StepStatus.PENDING
            current_step.inputs["resumed"] = True
            logger.info(f"Resuming interrupted step: {current_step.step_id} ({current_step.step_type.value})")
            persistence.save_flow(flow)

        # Display flow info with full content
        content = [
            f"Resuming flow: {flow.flow_id}",
            f"Current step: {flow.state.current_step_id}",
            f"Task: {flow.task_description}",
        ]
        render_full("\n".join(content), title="Flow Info")
    else:
        if not task_description:
            display_error("Task description required for new flow")
            return 1

        flow = state_machine.create_flow(
            task_description=task_description,
            task_type=task_type,
            change_name=change_name,
            is_loop_mode=is_loop_mode,
        )

        # Store explicit_type if user provided --type flag
        if task_type and task_type != "pending":
            flow.state.context["explicit_type"] = task_type
            persistence.save_flow(flow)

        # Display new flow info with full content
        content = [
            f"Created new flow: {flow.flow_id}",
            f"Task: {task_description}",
        ]

        # Only show type if explicitly provided (pending is auto-detect)
        if task_type and task_type != "pending":
            content.append(f"Type: {task_type} (user-specified)")
        else:
            content.append("Type: pending (will be determined by analyze)")

        if change_name:
            content.append(f"Change: {change_name}")
        render_full("\n".join(content), title="New Flow")

    # Execute flow
    while flow.status not in (FlowStatus.COMPLETED, FlowStatus.FAILED):
        current_step = flow.state.get_current_step()
        if not current_step:
            render_full("No current step, marking flow as complete", title="Info")
            flow.status = FlowStatus.COMPLETED
            break

        # Display step header with type information only when appropriate
        # Skip header for CONFIRM steps — the prompt speaks for itself
        step_type_value = current_step.step_type.value
        if current_step.step_type != StepType.CONFIRM:
            show_type = _should_show_type(step_type_value, flow)
            display_type = _get_display_task_type(flow) if show_type else None

            step_header_lines = [
                f"Step: {step_type_value}",
                f"Status: {current_step.status.value}",
            ]
            if display_type:
                step_header_lines.append(f"Type: {display_type}")
            elif flow.state.is_type_pending():
                step_header_lines.append("Type: pending")

            render_full(
                "\n".join(step_header_lines),
                title="Current Step"
            )

        step_start_time = datetime.now()

        # Special handling for CONFIRM steps on resume - check for existing response
        if current_step.step_type == StepType.CONFIRM and flow_id and current_step.status == StepStatus.PAUSED:
            existing_result = _check_confirm_response(flow, current_step, project_root)
            if existing_result:
                render_full(
                    f"Found existing confirmation response: {existing_result.value}",
                    title="Confirmation"
                )
                result = existing_result
            else:
                try:
                    result = state_machine.run_step(flow, current_step)
                except KeyboardInterrupt:
                    result = _handle_step_interrupt(flow, current_step, persistence, prompt_history)
                    if result is None:
                        return 130
                    continue
        else:
            try:
                result = state_machine.run_step(flow, current_step)
            except KeyboardInterrupt:
                result = _handle_step_interrupt(flow, current_step, persistence, prompt_history)
                if result is None:
                    return 130
                continue

        # Display step output with full content (skip for CONFIRM and DISCOVERY — handled by prompt)
        if current_step.step_type not in (StepType.CONFIRM, StepType.DISCOVERY, StepType.PLAN_TASKS):
            _display_step_output(current_step)

        # Handle CONFIRM step PAUSED state - prompt user for approval
        if current_step.step_type == StepType.CONFIRM and result == StepStatus.PAUSED:
            confirm_result = _handle_confirm_pause(flow, current_step, persistence, project_root, prompt_history)
            if confirm_result is None:
                # User chose to exit
                return 130
            # Re-run confirm step to process the response
            current_step.status = StepStatus.PENDING
            persistence.save_flow(flow)
            continue

        # Handle discovery step PAUSED state - need user input to continue
        if current_step.step_type == StepType.DISCOVERY and result == StepStatus.PAUSED:
            # Discovery is waiting for user response
            user_response = _handle_discovery_pause(flow, current_step, persistence, prompt_history)

            if user_response is None:
                # User chose to exit
                return 130

            if user_response == "__RESUME__":
                # User is resuming from a saved state, re-run the step
                continue

            # Store user response and re-run discovery step
            current_step.inputs["user_response"] = user_response
            current_step.inputs["resumed"] = True
            current_step.status = StepStatus.PENDING
            persistence.save_flow(flow)
            continue

        if result == StepStatus.FAILED:
            error_msg = current_step.error_message or "Unknown error"
            display_error(f"Step failed: {error_msg}")

            max_retries = 3
            if current_step.retry_count >= max_retries:
                display_error(
                    f"Max retries ({max_retries}) reached for step {current_step.step_type.value}"
                )
                # Auto-fail: exit without asking user
                flow.status = FlowStatus.FAILED
                persistence.save_flow(flow)
                return 1

            # Ask user whether to retry, skip, or abort
            options = ["Retry this step", "Skip to next step", "Abort flow"]
            choice = prompt_user_choice("What would you like to do?", options)

            if choice == 0:
                # Reset step status and retry
                current_step.status = StepStatus.PENDING
                current_step.retry_count += 1
                persistence.save_flow(flow)
                continue
            elif choice == 1:
                # Force step to completed so transition works
                current_step.status = StepStatus.COMPLETED
                state_machine.transition_to_next(flow)
                persistence.save_flow(flow)
                continue
            else:
                flow.status = FlowStatus.FAILED
                persistence.save_flow(flow)
                return 1

        # Handle REVISION_NEEDED status from CONFIRM step
        if result == StepStatus.REVISION_NEEDED:
            render_full(
                "Revision requested - transitioning to previous step",
                title="Revision"
            )
            # Mark the CONFIRM step as completed with revision info
            current_step.status = StepStatus.REVISION_NEEDED
            # Transition will handle going back to the previous step
            state_machine.transition_to_next(flow)
            persistence.save_flow(flow)
            continue

        step_duration = (datetime.now() - step_start_time).total_seconds()
        render_full(
            f"Step completed: {current_step.step_type.value} ({step_duration:.1f}s)",
            title="Progress"
        )

        # Transition to next step
        state_machine.transition_to_next(flow)
        persistence.save_flow(flow)

    # Flow complete
    if flow.status == FlowStatus.COMPLETED:
        display_success("Flow completed successfully!")
        return 0
    elif flow.status == FlowStatus.FAILED:
        current_step = flow.state.get_current_step()
        error_msg = current_step.error_message if current_step else "Unknown error"
        display_error(f"Flow failed: {error_msg}")
        return 1
    else:
        render_full(f"Flow ended with status: {flow.status.value}", title="Status")
        return 0


def run_loop_mode(
    project_root: Path,
    initial_task: Optional[str] = None,
    task_type: str = "pending",
    max_iterations: Optional[int] = None,
    prompt_history: Any = None,
    no_worktree: bool = False,
    merge_branch: Optional[str] = None,
) -> int:
    """Run in loop mode - continuously find and execute tasks.

    Delegates to LoopController for all loop lifecycle management.
    This function handles user interaction (display, prompts) while
    the controller manages state, worktree, and task tracking.

    Args:
        project_root: Project root directory
        initial_task: Optional initial task to start with
        task_type: Type of tasks to look for (default 'pending' for auto-detect)
        max_iterations: Maximum number of iterations (None for unlimited)
        prompt_history: Prompt input history
        no_worktree: If True, disable branch isolation (run on current branch)
        merge_branch: If provided, merge this loop branch and exit

    Returns:
        Exit code
    """
    from ..engine.loop_controller import LoopController
    from ..engine.worktree import get_current_branch, get_diff_stat

    controller = LoopController(
        project_root=project_root,
        max_iterations=max_iterations,
        no_worktree=no_worktree,
        prompt_history=prompt_history,
    )

    # --merge: merge an existing loop branch and exit
    if merge_branch:
        return _handle_merge_existing(controller, project_root, merge_branch)

    # Start: create branch/worktree
    setup_ok = controller.start()
    if setup_ok and controller.use_worktree and controller.has_worktree:
        render_full(
            "SE3 Loop Mode (branch isolated)\n\n"
            f"Branch: {controller.loop_branch}\n"
            f"Worktree: {controller.worktree_path}\n"
            f"Original: {controller.original_branch}\n\n"
            "Tasks execute in the worktree. Changes merge back when done.",
            title="Loop Mode"
        )
    elif not controller.use_worktree:
        if no_worktree:
            render_full(
                "SE3 Loop Mode (no isolation)\n\n"
                "Tasks run directly on the current branch.",
                title="Loop Mode"
            )
        else:
            display_error("Falling back to non-isolated mode (--no-worktree)")
            render_full(
                "SE3 Loop Mode\n\n"
                "WARNING: Running without branch isolation.",
                title="Loop Mode"
            )

    # Iteration loop
    current_task = initial_task
    iter_limit = max_iterations if max_iterations and max_iterations > 0 else 2**31
    interrupted = False

    try:
        for iteration in range(1, iter_limit + 1):
            render_full(f"Loop iteration #{iteration}", title="Iteration")

            result = controller.run_iteration(
                run_flow_fn=run_flow,
                task=current_task,
                task_type=task_type,
            )

            if result.task is None:
                render_full("No more tasks found. Loop mode complete.", title="Done")
                break

            render_full(f"Task: {result.task}", title="Current Task")

            if not result.success:
                display_error(f"Task failed with exit code {result.exit_code}")
                options = ["Continue to next task", "Exit loop mode"]
                choice = prompt_user_choice("What would you like to do?", options)
                if choice == 1:
                    break

            current_task = None
            render_full("Task complete. Looking for next task...", title="Progress")

            if max_iterations is not None and iteration >= max_iterations:
                display_success(
                    f"Loop mode completed: Reached maximum iterations ({max_iterations})"
                )
                break

    except KeyboardInterrupt:
        interrupted = True
        render_full("Loop interrupted by user.", title="Interrupted")

    # Post-loop: handle merge/cleanup
    return _handle_loop_finish(controller, interrupted)


def _handle_merge_existing(controller, project_root: Path, merge_branch: str) -> int:
    """Handle --merge flag: show diff summary, confirm, then merge."""
    from ..engine.worktree import get_current_branch, get_diff_stat

    target = get_current_branch(project_root)
    diff_stat = get_diff_stat(project_root, merge_branch, target)

    render_full(f"Merging loop branch: {merge_branch}\n\n{diff_stat}", title="Merge")

    options = [f"Merge {merge_branch} into {target}", "Cancel"]
    choice = prompt_user_choice("Proceed with merge?", options)
    if choice == 1:
        render_full("Merge cancelled.", title="Cancelled")
        return 0

    success = controller.merge_existing(merge_branch)
    if success:
        display_success(f"Successfully merged {merge_branch} into {target}")
        return 0
    else:
        display_error("Merge failed (conflict?). Resolve manually and retry.")
        return 1


def _handle_loop_finish(controller, interrupted: bool) -> int:
    """Handle post-loop merge/discard/defer decisions."""
    from ..engine.worktree import delete_branch

    finish_state = controller.finish(interrupted=interrupted)

    if not finish_state["loop_branch"] or not finish_state["original_branch"]:
        render_full("Loop mode ended.", title="Done")
        return 0

    loop_branch = finish_state["loop_branch"]
    original_branch = finish_state["original_branch"]

    if interrupted:
        render_full(
            f"Loop interrupted. Branch preserved: {loop_branch}\n\n"
            f"To merge later:\n"
            f"  se3 run --loop --merge {loop_branch}\n\n"
            f"To discard:\n"
            f"  git branch -D {loop_branch}",
            title="Interrupted"
        )
        return 0

    if finish_state["has_commits"]:
        options = [
            f"Merge {loop_branch} into {original_branch} now",
            "Merge later (keep branch)",
            "Discard (delete branch)",
        ]
        choice = prompt_user_choice(
            f"Loop complete. What to do with branch {loop_branch}?",
            options,
        )

        if choice == 0:
            success = controller.merge()
            if success:
                display_success(f"Merged {loop_branch} into {original_branch}")
            else:
                display_error(
                    f"Merge conflict. Branch preserved: {loop_branch}\n"
                    f"Resolve conflicts and merge manually."
                )
        elif choice == 1:
            render_full(
                f"Branch preserved: {loop_branch}\n\n"
                f"To merge later:\n"
                f"  se3 run --loop --merge {loop_branch}",
                title="Deferred"
            )
        else:
            controller.discard()
            display_success("Loop branch discarded.")
    else:
        controller.discard()
        render_full("Loop ended with no changes. Branch cleaned up.", title="Done")

    return 0


## CLI entry point is in cli.py (@app.command("run"))
## This module provides the logic functions: run_flow, run_loop_mode, etc.


if __name__ == "__main__":
    app()
