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
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer

# Add engine to path if needed
try:
    from ..engine.models import FlowInstance, FlowStatus, StepStatus, StepType
    from ..engine.persistence import PersistenceManager
    from ..engine.state_machine import StateMachine
    from ..engine.context_builder import ContextBuilder
    from ..engine.steps import STEP_HANDLERS
except ImportError:
    # Direct import for development
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from engine.models import FlowInstance, FlowStatus, StepStatus, StepType
    from engine.persistence import PersistenceManager
    from engine.state_machine import StateMachine
    from engine.context_builder import ContextBuilder
    from engine.steps import STEP_HANDLERS


app = typer.Typer()
logger = logging.getLogger(__name__)

SE3_DIR = ".se3"
STATE_FILE = "state/engine.json"


def get_project_root() -> Path:
    """Find project root by looking for .git directory."""
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".git").exists():
            return parent
    return cwd


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
                "description": data.get("task_description", "No description")[:60],
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
        print("No existing flows found. Starting new flow.")
        return None

    # Filter to active (non-terminal) flows
    terminal_statuses = {FlowStatus.COMPLETED.value, FlowStatus.FAILED.value}
    active_flows = [f for f in flows if f["status"] not in terminal_statuses]

    if not active_flows:
        print("No in-progress flows found.")
        if flows:
            print(f"Found {len(flows)} completed/failed flows.")
        return None

    if len(active_flows) == 1:
        flow = active_flows[0]
        print(f"\nFound interrupted flow:")
        print(f"  ID: {flow['id']}")
        print(f"  Description: {flow['description']}")
        print(f"  Current step: {flow['current_step']}")

        options = ["Resume this flow", "Start new flow"]
        choice = prompt_user_choice("What would you like to do?", options)

        if choice == 0:
            return flow["id"]
        return None

    # Multiple active flows
    print(f"\nFound {len(active_flows)} interrupted flows:")
    options = []
    for flow in active_flows:
        options.append(f"{flow['description']}... (step: {flow['current_step']})")
    options.append("Start new flow")

    choice = prompt_user_choice("Which flow to resume?", options)

    if choice < len(active_flows):
        return active_flows[choice]["id"]
    return None


def run_flow(
    project_root: Path,
    flow_id: Optional[str] = None,
    task_description: Optional[str] = None,
    task_type: str = "feature",
    change_name: Optional[str] = None,
    is_loop_mode: bool = False,
) -> int:
    """Run a flow to completion.

    Args:
        project_root: Project root directory
        flow_id: Flow ID to resume (None for new flow)
        task_description: Task description for new flow
        task_type: Type of task (feature, bugfix, etc.)
        change_name: Optional change name
        is_loop_mode: Whether to run in loop mode

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Initialize components
    persistence = PersistenceManager(project_root)
    state_machine = StateMachine(project_root, persistence)

    # Register all step handlers
    for step_type, handler in STEP_HANDLERS.items():
        state_machine.register_handler(step_type, handler)

    # Load or create flow
    if flow_id:
        flow = persistence.load_flow()
        if not flow or flow.flow_id != flow_id:
            print(f"Error: Flow '{flow_id}' not found", file=sys.stderr)
            return 1
        print(f"Resuming flow: {flow.flow_id}")
        print(f"Current step: {flow.state.current_step_id}")
        print(f"Task: {flow.task_description[:60]}...")
    else:
        if not task_description:
            print("Error: Task description required for new flow", file=sys.stderr)
            return 1

        flow = state_machine.create_flow(
            task_description=task_description,
            task_type=task_type,
            change_name=change_name,
            is_loop_mode=is_loop_mode,
        )
        print(f"Created new flow: {flow.flow_id}")
        print(f"Task: {task_description}")
        print(f"Type: {task_type}")

    # Execute flow
    try:
        while flow.status not in (FlowStatus.COMPLETED, FlowStatus.FAILED):
            current_step = flow.state.get_current_step()
            if not current_step:
                print("No current step, marking flow as complete")
                flow.status = FlowStatus.COMPLETED
                break

            print(f"\n{'='*60}")
            print(f"Step: {current_step.step_type.value}")
            print(f"Status: {current_step.status.value}")
            print(f"{'='*60}")

            result = state_machine.run_step(flow, current_step)

            if result == StepStatus.FAILED:
                error_msg = current_step.error_message or "Unknown error"
                print(f"Step failed: {error_msg}", file=sys.stderr)

                max_retries = 3
                if current_step.retry_count >= max_retries:
                    print(f"Max retries ({max_retries}) reached for step {current_step.step_type.value}", file=sys.stderr)
                    # Only offer skip or abort
                    options = ["Skip to next step", "Abort flow"]
                    choice = prompt_user_choice("What would you like to do?", options)
                    if choice == 0:
                        current_step.status = StepStatus.COMPLETED
                        state_machine.transition_to_next(flow)
                        persistence.save_flow(flow)
                        continue
                    else:
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

            print(f"Step completed: {current_step.step_type.value}")

            # Transition to next step
            state_machine.transition_to_next(flow)
            persistence.save_flow(flow)

        # Flow complete
        if flow.status == FlowStatus.COMPLETED:
            print(f"\n{'='*60}")
            print("Flow completed successfully!")
            print(f"{'='*60}")
            return 0
        elif flow.status == FlowStatus.FAILED:
            current_step = flow.state.get_current_step()
            error_msg = current_step.error_message if current_step else "Unknown error"
            print(f"\nFlow failed: {error_msg}", file=sys.stderr)
            return 1
        else:
            print(f"\nFlow ended with status: {flow.status.value}")
            return 0

    except KeyboardInterrupt:
        print("\n\nInterrupted by user. Flow state saved.")
        print(f"Resume with: se3 run --resume")
        return 130  # Standard exit code for Ctrl+C


def run_loop_mode(
    project_root: Path,
    initial_task: Optional[str] = None,
    task_type: str = "feature",
) -> int:
    """Run in loop mode - continuously find and execute tasks.

    Args:
        project_root: Project root directory
        initial_task: Optional initial task to start with
        task_type: Type of tasks to look for

    Returns:
        Exit code
    """
    print("="*60)
    print("SE3 Loop Mode")
    print("="*60)
    print("Loop mode will automatically find and execute tasks.")
    print("Each task runs in an isolated branch.")
    print()

    iteration = 0
    current_task = initial_task

    while True:
        iteration += 1
        print(f"\n{'='*60}")
        print(f"Loop iteration #{iteration}")
        print(f"{'='*60}")

        if not current_task:
            # Find next task from backlog/roadmap
            current_task = find_next_task(project_root)

            if not current_task:
                print("No more tasks found. Loop mode complete.")
                break

        print(f"Task: {current_task}")

        # Run the flow for this task
        exit_code = run_flow(
            project_root=project_root,
            task_description=current_task,
            task_type=task_type,
            is_loop_mode=True,
        )

        if exit_code != 0:
            print(f"\nTask failed with exit code {exit_code}")
            options = ["Continue to next task", "Exit loop mode"]
            choice = prompt_user_choice("What would you like to do?", options)

            if choice == 1:
                break

        # Clear for next iteration
        current_task = None

        print("\nTask complete. Looking for next task...")

    print("\nLoop mode ended.")
    return 0


def find_next_task(project_root: Path) -> Optional[str]:
    """Find the next task from backlog or roadmap.

    Args:
        project_root: Project root directory

    Returns:
        Task description or None if no tasks found
    """
    # Check for backlog files
    backlog_dir = project_root / "openspec" / "backlog"
    if backlog_dir.exists():
        for backlog_file in sorted(backlog_dir.glob("*.md")):
            try:
                content = backlog_file.read_text()
                # Simple parsing: look for unchecked items
                for line in content.split("\n"):
                    line = line.strip()
                    if line.startswith("- [ ]") or line.startswith("* [ ]"):
                        # Extract task description
                        task = line[5:].strip()
                        if task and len(task) > 5:
                            return f"[{backlog_file.stem}] {task}"
            except IOError:
                continue

    # Check for roadmap.md
    roadmap_file = project_root / "roadmap.md"
    if roadmap_file.exists():
        try:
            content = roadmap_file.read_text()
            # Look for current phase unchecked items
            in_current_phase = False
            for line in content.split("\n"):
                if "Phase 1" in line or "Current" in line.lower():
                    in_current_phase = True
                elif line.startswith("## Phase") and in_current_phase:
                    in_current_phase = False

                if in_current_phase and (line.strip().startswith("- [ ]") or line.strip().startswith("* [ ]")):
                    task = line.strip()[5:].strip()
                    if task and len(task) > 5:
                        return f"[roadmap] {task}"
        except IOError:
            pass

    # Check for TODO comments in code
    print("Scanning for TODOs in codebase...")
    try:
        result = subprocess.run(
            ["git", "grep", "-n", "TODO", "--", "*.py", "*.md"],
            cwd=project_root,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().split("\n")
            if lines:
                first_todo = lines[0].split(":", 2)
                if len(first_todo) >= 3:
                    return f"[TODO] {first_todo[2].strip()[:80]}"
    except Exception:
        pass

    return None


## CLI entry point is in cli.py (@app.command("run"))
## This module provides the logic functions: run_flow, run_loop_mode, etc.


if __name__ == "__main__":
    app()