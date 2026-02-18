"""SE3 Loop command — repeatedly run full-cycle workflow.

This command runs the se3 workflow in a loop for a specified number
of iterations, creating a new change for each iteration.

Usage:
    se3 loop "prompt" [--iterations 10]

Each iteration:
1. Checks if there's an incomplete change from previous iteration
2. If complete or no change: creates new change with the prompt
3. Returns control to agent for implementation
4. On next call: continues to next iteration
"""

import json
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime


def sanitize_change_name(description: str) -> str:
    """Convert a description into a valid change name."""
    name = description.lower().strip()
    name = "".join(c for c in name if c.isalnum() or c in " -_/")
    name = name.replace(" ", "-").replace("_", "-")
    while "--" in name:
        name = name.replace("--", "-")
    if len(name) > 40:
        name = name[:40].rsplit("-", 1)[0]
    return name.strip("-")


def load_loop_state(project_root: Path) -> Dict[str, Any]:
    """Load the loop state file."""
    state_file = project_root / ".se3-loop-state.json"
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except:
            pass
    return {
        "current_iteration": 0,
        "total_iterations": 10,
        "base_prompt": "",
        "changes": [],
        "status": "idle",  # idle, working, complete
    }


def save_loop_state(project_root: Path, state: Dict[str, Any]) -> None:
    """Save the loop state file."""
    state_file = project_root / ".se3-loop-state.json"
    state_file.write_text(json.dumps(state, indent=2, default=str))


def check_incomplete_changes(project_root: Path) -> Optional[Dict[str, Any]]:
    """Check if there are incomplete changes from previous iteration."""
    changes_dir = project_root / "openspec" / "changes"
    if not changes_dir.exists():
        return None

    for change_path in changes_dir.iterdir():
        if not change_path.is_dir() or change_path.name == "archive":
            continue

        state_file = change_path / ".se3-state.json"
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
                if not state.get("complete", False):
                    return {
                        "name": change_path.name,
                        "path": str(change_path),
                        "current_step": state.get("current_step"),
                        "workflow": state.get("workflow"),
                    }
            except:
                continue

    return None


def run_loop_iteration(
    prompt: str,
    project_root: str = ".",
    iterations: int = 10,
    quick: bool = False,
) -> Dict[str, Any]:
    """Run one iteration of the loop.

    This function:
    1. Loads or initializes loop state
    2. Checks for incomplete changes from previous iteration
    3. If no incomplete changes and iterations remain: creates new change
    4. Returns action for agent to execute
    """
    from .work import WORKFLOWS, StepStatus

    root = Path(project_root).resolve()
    result = {
        "prompt": prompt,
        "iterations": iterations,
        "project_root": str(root),
        "iteration": 0,
        "total_iterations": iterations,
        "change": None,
        "actions": [],
        "status": "idle",
        "complete": False,
    }

    # Load loop state
    loop_state = load_loop_state(root)

    # If this is a new loop (idle status), initialize it
    if loop_state["status"] == "idle":
        loop_state["base_prompt"] = prompt
        loop_state["total_iterations"] = iterations
        loop_state["current_iteration"] = 0
        loop_state["changes"] = []
        loop_state["status"] = "working"

    # Check for incomplete changes
    incomplete = check_incomplete_changes(root)
    if incomplete:
        result["iteration"] = loop_state["current_iteration"]
        result["total_iterations"] = loop_state["total_iterations"]
        result["change"] = incomplete
        result["status"] = "continue"
        result["actions"] = [{
            "type": "continue_work",
            "change": incomplete["name"],
            "reason": f"Continue working on incomplete change (iteration {loop_state['current_iteration']}/{loop_state['total_iterations']})",
        }]
        return result

    # Check if we've completed all iterations
    if loop_state["current_iteration"] >= loop_state["total_iterations"]:
        loop_state["status"] = "complete"
        save_loop_state(root, loop_state)
        result["iteration"] = loop_state["current_iteration"]
        result["total_iterations"] = loop_state["total_iterations"]
        result["status"] = "complete"
        result["complete"] = True
        result["actions"] = [{
            "type": "complete",
            "reason": f"All {loop_state['total_iterations']} iterations complete",
        }]
        return result

    # Start a new iteration
    loop_state["current_iteration"] += 1
    current_iter = loop_state["current_iteration"]

    # Generate change name
    base_name = sanitize_change_name(prompt)
    change_name = f"{base_name}-{current_iter:02d}"

    # Ensure unique name
    changes_dir = root / "openspec" / "changes"
    change_path = changes_dir / change_name
    counter = 1
    while change_path.exists():
        change_name = f"{base_name}-{current_iter:02d}-{counter}"
        change_path = changes_dir / change_name
        counter += 1

    # Create the change
    workflow_type = "small" if quick else "feature"
    steps = WORKFLOWS.get(workflow_type, WORKFLOWS["feature"])

    change_path.mkdir(parents=True, exist_ok=True)

    state = {
        "workflow": workflow_type,
        "current_step": steps[0],
        "steps": {step: StepStatus.PENDING.value for step in steps},
        "step_history": [],
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "description": f"{prompt} (Iteration {current_iter}/{iterations})",
        "loop_iteration": current_iter,
        "loop_total": iterations,
    }

    state_file = change_path / ".se3-state.json"
    state_file.write_text(json.dumps(state, indent=2, default=str))

    # Create minimal tasks.md
    tasks_file = change_path / "tasks.md"
    tasks_file.write_text(f"# {prompt} (Iteration {current_iter}/{iterations})\n\n## Tasks\n\n- [ ] {prompt}\n")

    # Update loop state
    loop_state["changes"].append({
        "iteration": current_iter,
        "name": change_name,
        "created_at": datetime.now().isoformat(),
    })
    save_loop_state(root, loop_state)

    # Build result
    result["iteration"] = current_iter
    result["total_iterations"] = iterations
    result["change"] = {
        "name": change_name,
        "path": str(change_path),
        "workflow": workflow_type,
    }
    result["status"] = "new_iteration"
    result["actions"] = [
        {
            "type": "implement",
            "description": prompt,
            "change": change_name,
            "reason": f"Loop iteration {current_iter}/{iterations}: Implement the change",
        },
        {
            "type": "run_tests",
            "reason": "Verify implementation before next iteration",
        },
        {
            "type": "commit",
            "reason": f"Commit iteration {current_iter} and continue to next",
        },
        {
            "type": "loop_continue",
            "reason": f"Run 'se3 loop' again for iteration {current_iter + 1}",
        },
    ]

    return result


def print_text_report(result: Dict[str, Any]) -> None:
    """Print a human-readable loop report."""
    print(f"\n{'=' * 60}")
    print("SE 3.0 Loop")
    print(f"{'=' * 60}")

    print(f"\nPrompt: {result.get('prompt', 'N/A')}")
    print(f"Iteration: {result.get('iteration', 0)}/{result.get('total_iterations', 0)}")

    status = result.get('status', 'unknown')
    print(f"\n{'-' * 60}")
    print(f"Status: {status.upper()}")
    print(f"{'-' * 60}")

    if status == "continue":
        change = result.get('change', {})
        print(f"\nContinue working on: {change.get('name', 'N/A')}")
        print(f"Workflow: {change.get('workflow', 'N/A')}")
        print(f"\nComplete this change before running 'se3 loop' again.")

    elif status == "new_iteration":
        change = result.get('change', {})
        print(f"\nNew change created: {change.get('name', 'N/A')}")
        print(f"Workflow: {change.get('workflow', 'N/A')}")

        print(f"\n{'-' * 60}")
        print("Action Sequence:")
        print(f"{'-' * 60}")
        for i, action in enumerate(result.get('actions', []), 1):
            print(f"\n{i}. [{action['type']}] {action.get('reason', '')}")
            if 'description' in action:
                print(f"   Description: {action['description']}")
            if 'change' in action:
                print(f"   Change: {action['change']}")

    elif status == "complete":
        print(f"\nAll iterations complete!")
        print(f"Total iterations: {result.get('total_iterations', 0)}")

    print(f"\n{'=' * 60}\n")


def print_json_report(result: Dict[str, Any]) -> None:
    """Print JSON loop report."""
    print(json.dumps(result, indent=2, default=str))
