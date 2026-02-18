"""SE3 Full-Cycle command — run start, work, and done in one command.

This command streamlines simple/quick tasks by running the complete
start-work-done workflow in a single command.

Usage:
    se3 full-cycle "description of work" [--quick]
"""

import json
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

import typer

from .start import run_session_start, create_session_file
from .work import run_work, WORKFLOWS, StepStatus
from .done import run_session_done

app = typer.Typer()


def sanitize_change_name(description: str) -> str:
    """Convert a description into a valid change name.

    Only lowercase letters, numbers, and hyphens are allowed.
    Non-ASCII characters (e.g., Chinese) are filtered out.
    Name must start with a letter.
    """
    import re
    import time

    name = description.lower().strip()
    # Keep only ASCII alphanumeric and allowed separators
    name = "".join(c for c in name if (ord(c) < 128 and c.isalnum()) or c in " -_/")
    name = name.replace(" ", "-").replace("_", "-").replace("/", "-")
    name = re.sub(r'-+', '-', name)
    if len(name) > 40:
        name = name[:40].rsplit("-", 1)[0]
    name = name.strip("-")
    # If name is empty (e.g., Chinese-only input), use timestamp-based fallback
    if not name:
        name = f"loop-{int(time.time()) % 10000}"
    # Ensure name starts with a letter (openspec requirement)
    if name and not name[0].isalpha():
        name = "t" + name
    return name


def detect_test_command(project_root: Path) -> Optional[str]:
    """Detect the test command for the project."""
    if (project_root / "pytest.ini").exists() or (project_root / "pyproject.toml").exists():
        return "python -m pytest tests/ -q"

    if (project_root / "package.json").exists():
        return "npm test"

    if (project_root / "Cargo.toml").exists():
        return "cargo test"

    if (project_root / "go.mod").exists():
        return "go test ./..."

    if (project_root / "tests").exists():
        return "python -m pytest tests/ -q"

    return None


def run_tests(project_root: Path) -> Dict[str, Any]:
    """Run tests and return results."""
    test_cmd = detect_test_command(project_root)
    if not test_cmd:
        return {"ran": False, "reason": "No test command detected"}

    result = subprocess.run(
        test_cmd.split(),
        cwd=project_root,
        capture_output=True,
        text=True
    )

    return {
        "ran": True,
        "cmd": test_cmd,
        "returncode": result.returncode,
        "passed": result.returncode == 0,
        "stdout": result.stdout[-1000:] if len(result.stdout) > 1000 else result.stdout,
        "stderr": result.stderr[-500:] if len(result.stderr) > 500 else result.stderr,
    }


def run_full_cycle(
    description: str,
    project_root: str = ".",
    quick: bool = False,
) -> Dict[str, Any]:
    """Run the full start-work-done cycle.

    This combines the three main SE3 commands into a single workflow
    optimized for simple, quick tasks.
    """
    root = Path(project_root).resolve()
    result = {
        "description": description,
        "quick_mode": quick,
        "project_root": str(root),
        "phases": {},
        "success": False,
        "actions": [],
    }

    # === PHASE 1: START ===
    start_result = run_session_start(project_root)
    result["phases"]["start"] = {
        "branch": start_result.get("git", {}).get("branch"),
        "uncommitted_count": start_result.get("git", {}).get("uncommitted_count", 0),
        "actions": start_result.get("actions", []),
    }

    # Handle any critical start actions
    if start_result.get("actions"):
        # Filter out non-critical actions
        critical_actions = [
            a for a in start_result["actions"]
            if a["type"] in ["ask_user", "run_script", "init_openspec"]
        ]
        if critical_actions:
            result["actions"].extend(critical_actions)
            result["error"] = "Start phase requires manual intervention"
            return result

    # === PHASE 2: WORK ===
    # Generate change name from description
    change_name = sanitize_change_name(description)

    # Determine workflow type
    workflow_type = "small" if quick else "feature"

    # Create the change
    openspec_dir = root / "openspec" / "changes"
    change_path = openspec_dir / change_name

    # If change already exists, append timestamp
    if change_path.exists():
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        change_name = f"{change_name}-{timestamp}"
        change_path = openspec_dir / change_name

    change_path.mkdir(parents=True, exist_ok=True)

    # Initialize workflow state
    steps = WORKFLOWS.get(workflow_type, WORKFLOWS["feature"])
    state = {
        "workflow": workflow_type,
        "current_step": steps[0],
        "steps": {step: StepStatus.PENDING.value for step in steps},
        "step_history": [],
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "description": description,
    }

    state_file = change_path / ".se3-state.json"
    state_file.write_text(json.dumps(state, indent=2, default=str))

    # Create a simple tasks.md for the work
    tasks_file = change_path / "tasks.md"
    tasks_file.write_text(f"# {description}\n\n## Tasks\n\n- [ ] {description}\n")

    result["phases"]["work"] = {
        "change": change_name,
        "workflow": workflow_type,
        "change_path": str(change_path),
    }

    # === PHASE 3: IMPLEMENTATION ===
    # The actual implementation is done by the agent
    # We just set up the structure and report what should happen

    implementation_actions = [
        {
            "type": "implement",
            "description": description,
            "reason": f"Full-cycle: Implement the requested change",
        },
        {
            "type": "run_tests",
            "reason": "Full-cycle: Verify implementation",
        },
    ]

    result["phases"]["implementation"] = {
        "actions": implementation_actions,
    }

    # === PHASE 4: DONE ===
    done_result = run_session_done(project_root)
    result["phases"]["done"] = {
        "uncommitted_changes": done_result.get("uncommitted_changes", {}),
        "actions": done_result.get("actions", []),
    }

    # Compute final actions
    final_actions = implementation_actions.copy()

    # Add commit action if there are changes
    uncommitted = done_result.get("uncommitted_changes", {})
    if uncommitted.get("has_changes"):
        final_actions.append({
            "type": "commit",
            "cmd": "se3 commit",
            "files": uncommitted.get("files", []),
            "reason": f"Full-cycle: Commit {uncommitted.get('count', 0)} changes",
        })

    # Add handoff action
    final_actions.append({
        "type": "handoff",
        "cmd": "se3 handoff",
        "reason": "Full-cycle: Complete session and handoff",
    })

    # Mark change as complete for quick mode
    if quick:
        state["current_step"] = None
        state["complete"] = True
        state["steps"] = {step: StepStatus.DONE.value for step in steps}
        state["updated_at"] = datetime.now().isoformat()
        state_file.write_text(json.dumps(state, indent=2, default=str))

    result["actions"] = final_actions
    result["success"] = True
    result["change_name"] = change_name

    return result


def print_text_report(result: Dict[str, Any]) -> None:
    """Print a human-readable full-cycle report."""
    print(f"\n{'=' * 60}")
    print("SE 3.0 Full-Cycle")
    print(f"{'=' * 60}")

    print(f"\nDescription: {result.get('description', 'N/A')}")
    print(f"Quick Mode: {'Yes' if result.get('quick_mode') else 'No'}")

    # Start phase
    start = result.get("phases", {}).get("start", {})
    print(f"\n{'-' * 60}")
    print("Phase 1: Start")
    print(f"{'-' * 60}")
    print(f"Branch: {start.get('branch', 'N/A')}")
    uncommitted = start.get('uncommitted_count', 0)
    if uncommitted > 0:
        print(f"Uncommitted: {uncommitted} files")
    else:
        print(f"Working Tree: clean")

    # Work phase
    work = result.get("phases", {}).get("work", {})
    print(f"\n{'-' * 60}")
    print("Phase 2: Work")
    print(f"{'-' * 60}")
    print(f"Change: {work.get('change', 'N/A')}")
    print(f"Workflow: {work.get('workflow', 'N/A')}")

    # Implementation phase
    impl = result.get("phases", {}).get("implementation", {})
    print(f"\n{'-' * 60}")
    print("Phase 3: Implementation")
    print(f"{'-' * 60}")
    impl_actions = impl.get("actions", [])
    for i, action in enumerate(impl_actions, 1):
        print(f"{i}. [{action['type']}] {action.get('reason', '')}")
        if 'description' in action:
            print(f"   {action['description']}")

    # Done phase
    done = result.get("phases", {}).get("done", {})
    print(f"\n{'-' * 60}")
    print("Phase 4: Done")
    print(f"{'-' * 60}")
    uncommitted_done = done.get("uncommitted_changes", {})
    if uncommitted_done.get("has_changes"):
        print(f"Changes to commit: {uncommitted_done.get('count', 0)} files")
    else:
        print(f"No uncommitted changes")

    # Final actions
    actions = result.get("actions", [])
    if actions:
        print(f"\n{'-' * 60}")
        print("Complete Action Sequence:")
        print(f"{'-' * 60}")
        for i, action in enumerate(actions, 1):
            print(f"\n{i}. [{action['type']}] {action.get('reason', '')}")
            if 'cmd' in action:
                print(f"   Command: {action['cmd']}")
            if 'description' in action:
                print(f"   Description: {action['description']}")

    # Status
    print(f"\n{'=' * 60}")
    if result.get("success"):
        print("Status: Ready to implement")
    else:
        print(f"Status: Blocked - {result.get('error', 'Unknown error')}")
    print(f"{'=' * 60}\n")


def print_json_report(result: Dict[str, Any]) -> None:
    """Print JSON full-cycle report."""
    print(json.dumps(result, indent=2, default=str))


@app.command()
def full_cycle(
    description: str = typer.Argument(..., help="Description of the work to do"),
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
    quick: bool = typer.Option(False, "--quick", "-q", help="Quick mode - skip formal change creation, use 'small' workflow"),
    format: str = typer.Option("text", "--format", "-f", help="Output format (text or json)"),
):
    """Run the complete start-work-done workflow in one command.

    This command streamlines simple/quick tasks by combining:
    1. se3 start - Initialize the session
    2. se3 work --new - Create a change for the work
    3. Implementation (performed by agent)
    4. se3 done - Complete the session

    Examples:
        se3 full-cycle "fix login bug"
        se3 full-cycle "add user profile page" --quick
        se3 full-cycle "update documentation" -q --json
    """
    result = run_full_cycle(description, project_root, quick)

    if format == "json":
        print_json_report(result)
    else:
        print_text_report(result)

    # Exit code: 0 = success, 1 = blocked/error
    raise typer.Exit(code=0 if result.get("success") else 1)
