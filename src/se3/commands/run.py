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
from rich.rule import Rule

# Add engine to path if needed
try:
    from ..engine.models import FlowInstance, FlowStatus, StepStatus, StepType
    from ..engine.persistence import PersistenceManager
    from ..engine.state_machine import StateMachine
    from ..engine.context_builder import ContextBuilder
    from ..engine.steps import STEP_HANDLERS
    from ..engine.steps.discovery import PROGRAMMATIC_CONFIRM_SENTINEL
    from ..engine.llm_caller import set_extra_prompt
    from ..config import ConfigError, clear_main_repo_root_cache
    from ..engine.output import (
        display_error,
        display_success,
        format_output,
        get_console,
        render_full,
    )
    from ..engine.step_renderers import render_step_output
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
    from engine.steps.discovery import PROGRAMMATIC_CONFIRM_SENTINEL
    from engine.llm_caller import set_extra_prompt
    from config import ConfigError, clear_main_repo_root_cache
    from engine.output import (
        display_error,
        display_success,
        format_output,
        get_console,
        render_full,
    )
    from engine.step_renderers import render_step_output
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
    """Find project root by looking for .git directory or an SE3 config file.

    When the current directory is inside a git worktree, this returns the
    worktree root (so SE3 state files remain isolated per-worktree).
    Config lookup via :func:`config.get_project_config_path` automatically
    ascends to the main repository when appropriate, so worktree-local
    ``se3.local.yaml`` still takes precedence and the main repo's
    ``se3.local.yaml`` can override the worktree's tracked ``se3.yaml``.
    """
    from ..config import is_se3_project_root

    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        # Check for .git directory
        if (parent / ".git").exists():
            return parent
        # Check for any SE3 project marker (se3.yaml, se3.local.yaml, se3.config.yaml)
        if is_se3_project_root(parent):
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

    # The reviewed step's output was already displayed by render_step_output
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
        get_console().print("[dim]No existing flows found. Starting new flow.[/dim]")
        return None

    # Filter to resumable flows (exclude only COMPLETED)
    terminal_statuses = {FlowStatus.COMPLETED.value}
    active_flows = [f for f in flows if f["status"] not in terminal_statuses]

    if not active_flows:
        get_console().print("[dim]No in-progress flows found.[/dim]")
        if flows:
            get_console().print(f"[dim]Found {len(flows)} completed flow(s).[/dim]")
        return None

    if len(active_flows) == 1:
        flow = active_flows[0]
        is_failed = flow["status"] == FlowStatus.FAILED.value
        label = "failed" if is_failed else "interrupted"
        content = [
            f"Found {label} flow:",
            "",
            f"  ID: {flow['id']}",
            f"  Description: {flow['description']}",
            f"  Current step: {flow['current_step']}",
        ]
        render_full("\n".join(content), title="Resume Flow")

        action = "Retry failed flow" if is_failed else "Resume this flow"
        options = [action, "Start new flow"]
        choice = prompt_user_choice("What would you like to do?", options)

        if choice == 0:
            return flow['id']
        return None

    # Multiple active flows
    content = [f"Found {len(active_flows)} resumable flows:", ""]
    options = []
    for flow in active_flows:
        status_tag = " [FAILED]" if flow["status"] == FlowStatus.FAILED.value else ""
        options.append(f"{flow['description']} (step: {flow['current_step']}){status_tag}")
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

    A non-empty user-typed instruction is persisted to
    ``flow.state.context["user_interjections"]`` and inlined into the
    current step's ``inputs["task_description"]`` so the immediate re-run
    sees it. Downstream steps pick up the same interjections via
    ``state_machine._build_step_inputs`` (which composes them onto every
    new step's task_description on construction).

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
        from datetime import datetime
        from ..engine.task_description import compose_task_description_with_interjections
        from ..engine.state_machine import _effective_task_description_base

        step_type_value = (
            current_step.step_type.value
            if hasattr(current_step.step_type, "value")
            else str(current_step.step_type)
        )
        entry = {
            "text": user_input,
            "step_id": current_step.step_id,
            "step_type": step_type_value,
            "timestamp": datetime.now().isoformat(),
        }
        flow.state.context.setdefault("user_interjections", []).append(entry)

        # Mutate the current step's inputs in-place so the immediate re-run
        # sees the new instruction. Compose from the *un-decorated* base
        # (refined_description if discovery ran, else flow.task_description)
        # against the FULL interjections list — never against the step's
        # already-composed task_description. Snapshotting the latter would
        # double the ``## Additional Instructions`` section when the user
        # interrupts a step that was built AFTER an earlier interjection
        # (its inputs.task_description already carries the section, and
        # composing on top of it would emit a second one).
        current_step.inputs["task_description"] = (
            compose_task_description_with_interjections(
                base=_effective_task_description_base(flow),
                interjections=flow.state.context["user_interjections"],
            )
        )
        get_console().print(
            "[dim]Additional instruction recorded — retrying step "
            "with persistent interjection.[/dim]"
        )
    else:
        get_console().print("[dim]Retrying step as-is...[/dim]")
    # Reset step to PENDING so it re-runs
    current_step.status = StepStatus.PENDING
    persistence.save_flow(flow)
    # Return a special marker to indicate retry
    return StepStatus.PENDING


def _restore_discovery_display(current_step: Any) -> None:
    """Re-display the last AI message from a PAUSED discovery step on resume.

    When a DISCOVERY step is PAUSED and we are resuming (rather than running
    for the first time), the LLM already asked its question — we must NOT
    call the LLM again.  This function re-prints the last assistant message
    stored in ``discovery_state`` so the user can see what was asked before
    they type their reply.

    Args:
        current_step: The discovery step with status PAUSED
    """
    from ..engine.steps.discovery import _display_discovery_message

    discovery_state = current_step.inputs.get("discovery_state", {})
    history = discovery_state.get("history", [])

    # Find the last assistant entry in the conversation history
    last_assistant = None
    for entry in reversed(history):
        if entry.get("role") == "assistant":
            last_assistant = entry
            break

    if last_assistant:
        # Use step.outputs["message"] for display (clean parsed content),
        # NOT history content (which is the full raw LLM result text for context).
        content = current_step.outputs.get("message", last_assistant.get("content", ""))
        # Re-display proposed_description if it was set
        # (confirmation mode stores refined_description instead)
        proposed = current_step.outputs.get("proposed_description") \
            or current_step.outputs.get("refined_description") \
            or None
        questions = current_step.outputs.get("questions") or None
        # Detect confirmation mode for proper rendering
        is_confirmation = current_step.outputs.get("awaiting_programmatic_confirm", False)
        # Pass the raw result text so narrative outside JSON blocks is rendered
        raw_result_text = last_assistant.get("content", "")
        _display_discovery_message(
            content, proposed, questions,
            is_confirmation=is_confirmation,
            raw_result_text=raw_result_text,
        )
    else:
        # No history yet — show generic resume notice
        get_console().print("[dim]Resuming discovery — please respond to continue.[/dim]")


def _handle_discovery_pause(flow: FlowInstance, current_step: Any, persistence: PersistenceManager, prompt_history: Any = None) -> Optional[str]:
    """Handle discovery step pause - get user response.

    Args:
        flow: Current flow instance
        current_step: The discovery step
        persistence: Persistence manager

    Returns:
        User response string, or None to exit
    """
    # Programmatic confirmation gate: LLM confirmed, now require human approval
    if current_step.outputs.get("awaiting_programmatic_confirm"):
        return _handle_discovery_programmatic_confirm(
            flow, current_step, persistence, prompt_history
        )

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
        # Empty input — ask again
        get_console().print("[yellow]Please provide a response or press Ctrl+C to exit.[/yellow]")
        return _handle_discovery_pause(flow, current_step, persistence, prompt_history)

    return user_input


# Sentinel returned by programmatic confirm handler when user chooses to proceed.
# INVARIANT: This value is only returned AFTER setting inputs["programmatic_confirmed"]=True.
# The discovery_handler's early-return guard (programmatic_confirmed check) MUST execute
# before any code path that could feed this value to the LLM as a user message. The
# persistence.save_flow() call in the orchestrator loop happens after the sentinel is
# returned but before the next handler invocation, so the flag is always persisted.
_PROGRAMMATIC_CONFIRM = PROGRAMMATIC_CONFIRM_SENTINEL


def _handle_discovery_programmatic_confirm(
    flow: FlowInstance,
    current_step: Any,
    persistence: PersistenceManager,
    prompt_history: Any = None,
) -> Optional[str]:
    """Handle programmatic confirmation gate after LLM confirms discovery.

    Uses the regular discovery multiline input box. The user types exactly
    "1" (strict equality, no whitespace stripping) to confirm and proceed.
    Empty input is a no-op (re-displays the confirmation panel). Any other
    non-empty input continues discovery as the next user turn.

    Args:
        flow: Current flow instance
        current_step: The discovery step
        persistence: Persistence manager
        prompt_history: Prompt history for readline

    Returns:
        _PROGRAMMATIC_CONFIRM sentinel if user confirms,
        user's input string if they want to continue discovery,
        or None if cancelled (Ctrl+C/EOF).
    """
    # The confirmation panel was already displayed by the discovery handler
    # or _restore_discovery_display.
    while True:
        user_input = _read_multiline_input(
            prompt_title="Discovery Confirmation",
            prompt_message="Type 1 to confirm and proceed, or type your questions/feedback to continue discovery (Ctrl+D or Esc+Enter to finish, Ctrl+C to cancel):",
            history=prompt_history,
            strip=False,
        )

        if user_input is None:
            # None = Ctrl+C (interactive) or EOF/empty pipe (non-interactive).
            # NOTE: Intentional divergence — interactive empty input (Ctrl+D on
            # empty buffer) returns "" and loops with re-display. Non-interactive
            # empty input returns None because sys.stdin.read() consumes all data
            # at once; there is nothing left to re-read, so pausing is the only
            # safe behavior. Scripted drivers that pipe "\n" or empty input will
            # see the flow pause rather than loop.
            persistence.save_flow(flow)
            render_full(
                "Discovery paused. Flow state saved.\n"
                "Resume with: se3 run --resume",
                title="Paused",
            )
            return None

        # Strip trailing newlines — these are artifacts of the multiline input
        # UI (pressing Enter before Ctrl+D), not part of the user's intended
        # input. The spec's strict == "1" rule still rejects " 1 ", "1.",
        # "yes", " 1", etc.; only the exact single character "1" confirms.
        if user_input.rstrip('\n\r') == "1":
            current_step.inputs["programmatic_confirmed"] = True
            return _PROGRAMMATIC_CONFIRM

        if not user_input.strip():
            # Empty or whitespace-only input — no-op: re-display the cached
            # confirmation panel. This covers both interactive empty input
            # and non-interactive piped whitespace (e.g., "   \n").
            from ..engine.steps.discovery import _display_discovery_message

            content = current_step.outputs.get("message", "")
            refined = (
                current_step.outputs.get("refined_description")
                or current_step.outputs.get("proposed_description")
                or ""
            )
            raw_result_text = current_step.outputs.get("raw_result_text", "")

            _display_discovery_message(
                content, refined, is_confirmation=True, raw_result_text=raw_result_text
            )
            continue

        # Non-empty, non-"1" input — continue discovery:
        # clear the programmatic confirm flag, use this input as the next
        # discovery user input directly (no separate prompt for questions)
        current_step.outputs.pop("awaiting_programmatic_confirm", None)
        get_console().print(
            "[dim]Captured input — continuing discovery with your feedback.[/dim]"
        )
        return user_input


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
    source_issue_id: Optional[str] = None,
) -> int:
    """Run a flow to completion.

    Args:
        project_root: Project root directory
        flow_id: Flow ID to resume (None for new flow)
        task_description: Task description for new flow
        task_type: Type of task (feature, bugfix, etc., or 'pending' to auto-detect)
        change_name: Optional change name
        is_loop_mode: Whether to run in loop mode
        source_issue_id: Optional issue ID that triggered this flow

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
            is_loop_mode, persistence, state_machine, prompt_history,
            source_issue_id=source_issue_id,
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
    source_issue_id: Optional[str] = None,
) -> int:
    """Internal implementation of flow execution."""
    # Register all step handlers
    for step_type, handler in STEP_HANDLERS.items():
        state_machine.register_handler(step_type, handler)

    # Load or create flow
    try:
        if flow_id:
            flow = persistence.load_flow()
            if not flow or flow.flow_id != flow_id:
                display_error(f"Flow '{flow_id}' not found")
                return 1

            # Detect and handle resume of a RUNNING or FAILED step
            current_step = flow.state.get_current_step()
            if current_step and current_step.status == StepStatus.RUNNING:
                # Step was interrupted - prepare for resumption with context
                current_step.status = StepStatus.PENDING
                current_step.inputs["resumed"] = True
                # Increment retry_count so LLMCaller picks up conversation history
                # from the interrupted run via _get_retry_context()
                current_step.inputs["retry_count"] = current_step.inputs.get("retry_count", 0) + 1
                current_step.retry_count = 0
                logger.info(f"Resuming interrupted step: {current_step.step_id} ({current_step.step_type.value})")
                persistence.save_flow(flow)
            elif current_step and current_step.status == StepStatus.FAILED:
                # Step failed - prepare for retry from breakpoint
                current_step.status = StepStatus.PENDING
                current_step.inputs["resumed"] = True
                # Increment retry_count so LLMCaller picks up conversation history (external_attempt)
                current_step.inputs["retry_count"] = current_step.inputs.get("retry_count", 0) + 1
                # Reset retry_count on the step model for fresh retry budget
                current_step.retry_count = 0
                flow.status = FlowStatus.RUNNING
                logger.info(f"Retrying failed step from breakpoint: {current_step.step_id} ({current_step.step_type.value})")
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

            # Set source issue ID if provided
            if source_issue_id:
                flow.source_issue_id = source_issue_id

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

        # Initialize flow metadata and baseline commit (idempotent — safe for both
        # new and resumed flows).
        state_machine.init_flow(flow)
    except ConfigError as exc:
        display_error(str(exc))
        return 2

    # Execute flow
    while flow.status not in (FlowStatus.COMPLETED, FlowStatus.FAILED):
        current_step = flow.state.get_current_step()
        if not current_step:
            get_console().print("[dim]No current step — marking flow complete[/dim]")
            flow.status = FlowStatus.COMPLETED
            break

        # If the current step already finished (process crashed after the step
        # handler returned but before transition_to_next was saved), advance
        # without re-running the step.
        if current_step.status in (StepStatus.COMPLETED, StepStatus.PARTIAL):
            logger.info(
                f"Step {current_step.step_type.value} already {current_step.status.value}, "
                "advancing to next step without re-running"
            )
            state_machine.transition_to_next(flow)
            persistence.save_flow(flow)
            continue

        # REVISION_NEEDED saved but transition not yet applied: call transition_to_next
        # which routes to _transition_to_fix (TEST/VERIFY_SPEC) or _transition_to_revision
        # (CONFIRM), without re-running the step and without double-incrementing counters.
        if current_step.status == StepStatus.REVISION_NEEDED:
            logger.info(
                f"Step {current_step.step_type.value} already REVISION_NEEDED, "
                "applying transition without re-running"
            )
            get_console().print(
                f"[yellow]Resuming: revision was already requested from "
                f"{current_step.step_type.value}[/yellow]"
            )
            state_machine.transition_to_next(flow)
            persistence.save_flow(flow)
            continue

        # Display compact step header — skip for CONFIRM steps (the prompt speaks for itself)
        step_type_value = current_step.step_type.value
        if current_step.step_type != StepType.CONFIRM:
            show_type = _should_show_type(step_type_value, flow)
            display_type = _get_display_task_type(flow) if show_type else None

            type_suffix = ""
            if display_type:
                type_suffix = f" [dim]({display_type})[/dim]"
            elif flow.state.is_type_pending():
                type_suffix = " [dim](pending)[/dim]"

            console = get_console()
            console.print(Rule(f"[bold]{step_type_value}[/bold]{type_suffix}", style="cyan"))

        step_start_time = datetime.now()

        # Special handling for CONFIRM steps on resume - check for existing response
        if current_step.step_type == StepType.CONFIRM and flow_id and current_step.status == StepStatus.PAUSED:
            existing_result = _check_confirm_response(flow, current_step, project_root)
            if existing_result:
                get_console().print(f"[dim]Found existing confirmation response: {existing_result.value}[/dim]")
                result = existing_result
            else:
                try:
                    result = state_machine.run_step(flow, current_step)
                except KeyboardInterrupt:
                    result = _handle_step_interrupt(flow, current_step, persistence, prompt_history)
                    if result is None:
                        return 130
                    continue

        # Special handling for DISCOVERY steps on resume - restore last AI message without
        # re-calling the LLM (the question was already asked; just wait for user input again)
        elif current_step.step_type == StepType.DISCOVERY and current_step.status == StepStatus.PAUSED:
            _restore_discovery_display(current_step)
            result = StepStatus.PAUSED

        else:
            try:
                result = state_machine.run_step(flow, current_step)
            except KeyboardInterrupt:
                result = _handle_step_interrupt(flow, current_step, persistence, prompt_history)
                if result is None:
                    return 130
                continue

        # Display step output with full content (skip for CONFIRM and DISCOVERY — handled by prompt)
        if current_step.step_type not in (StepType.CONFIRM, StepType.DISCOVERY, StepType.PLAN):
            render_step_output(current_step)

        # Handle CONFIRM step PAUSED state - prompt user for approval
        if current_step.step_type == StepType.CONFIRM and result == StepStatus.PAUSED:
            # Defensive guard: LLM reviewer should never return PAUSED
            if current_step.inputs.get('reviewer') == 'llm':
                logger.warning(
                    "BUG: LLM reviewer returned PAUSED — this should not happen. "
                    "Skipping interactive prompt."
                )
                current_step.status = StepStatus.PENDING
                persistence.save_flow(flow)
                continue
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

            # Store user response and re-run discovery step.
            # Advancing to the next discovery round is a NEW LLM call with a
            # new CONTINUE_DISCOVERY_PROMPT (containing the fresh
            # user_response and updated conversation history) — not a retry
            # of the previous round. Clear any stale retry counter left
            # behind by a prior FAILED-Retry or interrupted-resume so the
            # next round's prompt isn't discarded by LLMCaller's
            # retry-context wrapping.
            current_step.inputs["user_response"] = user_response
            current_step.inputs["resumed"] = True
            current_step.inputs.pop("retry_count", None)
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
                # Reset step status and retry from where it left off
                current_step.status = StepStatus.PENDING
                current_step.inputs["resumed"] = True
                current_step.inputs["retry_count"] = current_step.inputs.get("retry_count", 0) + 1
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
            get_console().print("[yellow]Revision requested — returning to previous step[/yellow]")
            # Mark the CONFIRM step as completed with revision info
            current_step.status = StepStatus.REVISION_NEEDED
            # Transition will handle going back to the previous step
            state_machine.transition_to_next(flow)
            persistence.save_flow(flow)
            continue

        step_duration = (datetime.now() - step_start_time).total_seconds()
        console = get_console()
        console.print(f"  [green]✓[/green] [bold]{current_step.step_type.value}[/bold] completed [dim]({step_duration:.1f}s)[/dim]")

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
        get_console().print(f"[dim]Flow ended with status: {flow.status.value}[/dim]")
        return 0


def _generate_iteration_summary(
    controller,
    result,
    iteration: int,
    project_root: Path,
) -> str:
    """Generate a structured iteration summary using LLM.

    Falls back to a simple status string if LLM call fails.
    """
    fallback = f"{'success' if result.success else 'failed'}"

    try:
        import subprocess as _sp

        # Collect git diff since iteration start
        diff_text = ""
        if controller.iteration_start_commit:
            diff_result = _sp.run(
                ["git", "-C", str(controller.effective_root),
                 "diff", controller.iteration_start_commit, "HEAD"],
                capture_output=True, text=True, timeout=30,
            )
            if diff_result.returncode == 0:
                diff_text = diff_result.stdout[:5000]

        # Collect test results from flow state if available
        test_output = ""
        # We don't have direct access to flow state here, so skip test output
        # Test result is partially reflected in result.success

        if not diff_text:
            return fallback

        from ..engine.llm_caller import LLMCaller

        prompt = (
            "Summarize this loop iteration in ≤200 words with three sections:\n"
            "1. What was done\n"
            "2. Test results\n"
            "3. Remaining issues\n\n"
            f"Task: {result.task}\n"
            f"Outcome: {'success' if result.success else 'failed'}\n\n"
            f"Git diff (truncated):\n```\n{diff_text}\n```"
        )

        caller = LLMCaller(project_root, step_type="iteration_summary")
        summary = caller.call(prompt=prompt)
        if summary and len(summary.strip()) > 10:
            return summary.strip()
        return fallback
    except Exception:
        return fallback


def run_loop_mode(
    project_root: Path,
    initial_task: str,
    task_type: str = "pending",
    max_iterations: Optional[int] = None,
    prompt_history: Any = None,
    no_worktree: bool = False,
    merge_branch: Optional[str] = None,
) -> int:
    """Run in Ralph Loop mode - repeat a user prompt across iterations.

    Delegates to LoopController for all loop lifecycle management.
    This function handles user interaction (display, prompts) while
    the controller manages state, worktree, and summary injection.

    Args:
        project_root: Project root directory
        initial_task: User prompt to execute each iteration (required)
        task_type: Type of tasks to look for (default 'pending' for auto-detect)
        max_iterations: Maximum number of iterations (None for unlimited)
        prompt_history: Prompt input history
        no_worktree: If True, disable branch isolation (run on current branch)
        merge_branch: If provided, merge this loop branch and exit

    Returns:
        Exit code
    """
    from ..engine.loop_controller import LoopController

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
    setup_ok = controller.start(task=initial_task)
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
    iter_limit = max_iterations if max_iterations and max_iterations > 0 else 2**31
    interrupted = False

    try:
        for iteration in range(1, iter_limit + 1):
            # Invalidate cached worktree topology before each iteration so
            # that config lookups reflect the current state (worktrees may
            # have been created or removed between iterations).
            #
            # Defensive assumption: config is loaded only at iteration
            # boundaries (here, before the implement step runs), NOT inside
            # per-group worktrees created by the DAG-parallel implement path.
            # If a future change adds config reads inside transient worktrees,
            # this single cache clear at the top of the loop would be
            # insufficient — an additional clear would be needed after each
            # per-group worktree is torn down.
            clear_main_repo_root_cache()

            get_console().print(Rule(f"[bold]Loop #{iteration}[/bold]", style="cyan"))

            result = controller.run_iteration(
                run_flow_fn=run_flow,
                task=initial_task,
                task_type=task_type,
            )

            if not result.success:
                display_error(f"Task failed with exit code {result.exit_code}")

            # Generate iteration summary for next round
            summary = _generate_iteration_summary(
                controller, result, iteration, project_root,
            )
            controller.add_summary(summary)
            controller.iteration_summary = summary

            if max_iterations is not None and iteration >= max_iterations:
                display_success(
                    f"Loop mode completed: Reached maximum iterations ({max_iterations})"
                )
                break

    except KeyboardInterrupt:
        interrupted = True
        get_console().print("[yellow]Loop interrupted by user.[/yellow]")

    # Post-loop: auto merge
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
    """Handle post-loop cleanup with automatic merge."""
    finish_state = controller.finish(interrupted=interrupted)

    if not finish_state["loop_branch"] or not finish_state["original_branch"]:
        get_console().print("[dim]Loop mode ended.[/dim]")
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
        success = controller.merge()
        if success:
            display_success(f"Merged {loop_branch} into {original_branch}")
        else:
            display_error(
                f"Merge conflict. Branch preserved: {loop_branch}\n"
                f"Resolve conflicts and merge manually."
            )
    else:
        controller.discard()
        get_console().print("[dim]Loop ended with no changes. Branch cleaned up.[/dim]")

    return 0


## CLI entry point is in cli.py (@app.command("run"))
## This module provides the logic functions: run_flow, run_loop_mode, etc.


if __name__ == "__main__":
    app()
