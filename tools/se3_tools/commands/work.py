"""SE3 Work command — the workflow driver for SDD development.

Encodes all SDD workflows (bugfix, feature, review, directive) into programmatic
logic with step tracking and adaptive formality.

Workflow state is persisted in openspec/changes/<name>/.se3-state.json
"""

import json
import re
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
from enum import Enum

import typer

app = typer.Typer()


class WorkflowType(str, Enum):
    BUGFIX = "bugfix"
    FEATURE = "feature"
    REVIEW = "review"
    DIRECTIVE = "directive"
    SMALL = "small"


# Workflow step definitions
WORKFLOWS = {
    "bugfix": ["analyze", "fix", "verify"],
    "feature": ["clarify", "propose", "spec", "design", "implement", "verify"],
    "review": ["inspect", "report", "fix"],
    "directive": ["plan", "implement", "verify", "check_coverage"],
    "small": ["implement", "verify"],
}


class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


def compute_git_status(project_root: Path) -> Dict[str, Any]:
    """Compute current git state."""
    info = {
        "branch": "unknown",
        "uncommitted_count": 0,
    }

    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=project_root, capture_output=True, text=True
    )
    if result.returncode == 0:
        info["branch"] = result.stdout.strip() or "(detached HEAD)"

    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_root, capture_output=True, text=True
    )
    if result.returncode == 0 and result.stdout.strip():
        lines = result.stdout.strip().split("\n")
        info["uncommitted_count"] = len(lines)

    return info


def detect_workflow_type(change_name: str, change_path: Path) -> str:
    """Detect the workflow type based on change name and contents."""
    # Check for explicit workflow in .se3-state.json
    state_file = change_path / ".se3-state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            if "workflow" in state:
                return state["workflow"]
        except (json.JSONDecodeError, OSError):
            pass

    # Infer from change name
    if change_name.startswith("bugfix/"):
        return "bugfix"
    if change_name.startswith("review/"):
        return "review"

    # Infer from contents
    has_proposal = (change_path / "proposal.md").exists()
    has_specs = (change_path / "specs").exists() and any((change_path / "specs").iterdir())
    has_design = (change_path / "design.md").exists()

    if has_proposal and has_specs and has_design:
        return "feature"  # Full ceremony
    elif has_proposal and has_specs:
        return "feature"  # Medium
    elif not has_proposal and not has_specs:
        # Simple change - check if it's small
        return "small"

    return "feature"


def read_change_state(change_path: Path) -> Optional[Dict[str, Any]]:
    """Read the workflow state from .se3-state.json."""
    state_file = change_path / ".se3-state.json"
    if not state_file.exists():
        return None

    try:
        return json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def write_change_state(change_path: Path, state: Dict[str, Any]) -> None:
    """Write the workflow state to .se3-state.json."""
    state_file = change_path / ".se3-state.json"
    state["updated_at"] = datetime.now().isoformat()
    state_file.write_text(json.dumps(state, indent=2, default=str))


def initialize_change_state(change_path: Path, change_name: str) -> Dict[str, Any]:
    """Initialize workflow state for a new change."""
    workflow = detect_workflow_type(change_name, change_path)
    steps = WORKFLOWS.get(workflow, WORKFLOWS["feature"])

    state = {
        "workflow": workflow,
        "current_step": steps[0],
        "steps": {step: StepStatus.PENDING.value for step in steps},
        "step_history": [],
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }

    write_change_state(change_path, state)
    return state


def read_tasks(change_path: Path) -> List[Dict[str, Any]]:
    """Read tasks from tasks.md in the change directory."""
    tasks_file = change_path / "tasks.md"
    if not tasks_file.exists():
        return []

    tasks = []
    content = tasks_file.read_text()

    # Parse markdown task list
    for line in content.split("\n"):
        # Match both checked and unchecked tasks
        match = re.match(r"^- \[(x| )\] (.+)", line)
        if match:
            tasks.append({
                "done": match.group(1) == "x",
                "description": match.group(2).strip(),
            })

    return tasks


def get_next_pending_task(tasks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Get the next pending (not done) task."""
    for i, task in enumerate(tasks):
        if not task.get("done", False):
            return {"index": i, **task}
    return None


def compute_step_actions(
    change_name: str,
    workflow: str,
    current_step: str,
    change_path: Path,
) -> List[Dict[str, Any]]:
    """Compute actions for the current workflow step."""
    actions = []

    if current_step == "analyze":
        actions.append({
            "type": "analyze_bug",
            "description": "Reproduce the bug, identify root cause, determine affected components",
            "reason": "Bug fix workflow step 1: Analysis",
        })

    elif current_step == "fix":
        # Check if this is a small fix (no openspec change) or formal change
        if workflow == "small":
            actions.append({
                "type": "implement",
                "description": "Fix directly in code",
                "reason": "Small bug fix - direct implementation",
            })
        else:
            actions.append({
                "type": "implement",
                "description": "Implement fix according to fix-spec.md",
                "reason": "Bug fix workflow step 2: Implementation",
            })
            actions.append({
                "type": "run_tests",
                "reason": "Verify fix works",
            })

    elif current_step == "verify":
        actions.append({
            "type": "run_tests",
            "reason": "Run regression tests to confirm fix",
        })
        actions.append({
            "type": "verify_scenarios",
            "reason": "Verify all spec scenarios pass",
        })
        if workflow != "small":
            actions.append({
                "type": "archive_change",
                "reason": "Archive completed change",
            })

    elif current_step == "clarify":
        actions.append({
            "type": "ask_user",
            "question": "Clarify requirements: What exactly should be built? What are the acceptance criteria?",
            "reason": "Feature workflow step 1: Clarification",
        })

    elif current_step == "propose":
        actions.append({
            "type": "write_proposal",
            "description": "Create proposal.md with what, why, acceptance criteria",
            "reason": "Feature workflow step 2: Proposal",
        })

    elif current_step == "spec":
        actions.append({
            "type": "write_spec",
            "description": "Write/update specs in openspec/specs/ with WHEN/THEN scenarios",
            "reason": "Feature workflow step 3: Specification",
        })
        actions.append({
            "type": "run_lint",
            "cmd": "se3 lint",
            "reason": "Validate specs",
        })

    elif current_step == "design":
        # Check if design is actually needed
        has_complexity = _check_change_complexity(change_path)
        if has_complexity:
            actions.append({
                "type": "write_design",
                "description": "Create design.md for complex architecture decisions",
                "reason": "Feature workflow step 4: Design (complex change)",
            })
        else:
            # Skip design step for simple changes
            actions.append({
                "type": "skip_step",
                "step": "design",
                "reason": "Simple change - no complex architecture decisions",
            })

    elif current_step == "implement":
        tasks = read_tasks(change_path)
        if tasks:
            next_task = get_next_pending_task(tasks)
            if next_task:
                actions.append({
                    "type": "implement_task",
                    "task_index": next_task["index"],
                    "description": next_task["description"],
                    "reason": f"Feature workflow step 5: Implement task {next_task['index'] + 1}/{len(tasks)}",
                })
                actions.append({
                    "type": "run_tests",
                    "reason": "Verify task implementation",
                })
            else:
                # All tasks done
                actions.append({
                    "type": "advance_step",
                    "reason": "All tasks complete - ready to verify",
                })
        else:
            # No tasks file - ask to create one or implement directly
            actions.append({
                "type": "write_tasks",
                "description": "Break work into tasks (max 5 per group)",
                "reason": "Feature workflow step 5: Task planning",
            })

    elif current_step == "inspect":
        actions.append({
            "type": "inspect_code",
            "description": "Read the code/file in question, check against specs",
            "reason": "Review workflow step 1: Inspection",
        })

    elif current_step == "report":
        actions.append({
            "type": "report_review",
            "description": "Provide findings categorized as critical/warning/suggestion",
            "reason": "Review workflow step 2: Report",
        })

    elif current_step == "plan":
        actions.append({
            "type": "create_change",
            "description": "Create openspec change from user direction",
            "reason": "Directive workflow step 1: Plan",
        })

    elif current_step == "check_coverage":
        actions.append({
            "type": "check_spec_coverage",
            "description": "Check if specs fully cover project goals",
            "reason": "Directive workflow step 4: Coverage check",
        })

    return actions


def _check_change_complexity(change_path: Path) -> bool:
    """Check if change is complex enough to need design doc."""
    # Check for indicators of complexity
    # - Multiple spec files
    # - Cross-cutting concerns mentioned in proposal
    # - Large number of tasks

    specs_dir = change_path / "specs"
    if specs_dir.exists():
        spec_count = len(list(specs_dir.rglob("*.md")))
        if spec_count > 2:
            return True

    proposal_file = change_path / "proposal.md"
    if proposal_file.exists():
        content = proposal_file.read_text()
        complexity_indicators = [
            "cross-cutting", "architecture", "refactor", "redesign",
            "breaking change", "migration", "multiple components"
        ]
        for indicator in complexity_indicators:
            if indicator in content.lower():
                return True

    tasks = read_tasks(change_path)
    if len(tasks) > 5:
        return True

    return False


def advance_step(state: Dict[str, Any], change_path: Path) -> Dict[str, Any]:
    """Advance to the next workflow step."""
    workflow = state.get("workflow", "feature")
    steps = WORKFLOWS.get(workflow, WORKFLOWS["feature"])
    current = state.get("current_step")

    # Record completion
    if current:
        state["step_history"].append({
            "step": current,
            "completed_at": datetime.now().isoformat(),
        })
        state["steps"][current] = StepStatus.DONE.value

    # Find next step
    if current in steps:
        idx = steps.index(current)
        if idx + 1 < len(steps):
            next_step = steps[idx + 1]
            state["current_step"] = next_step
            state["steps"][next_step] = StepStatus.IN_PROGRESS.value
        else:
            # All steps complete
            state["current_step"] = None
            state["complete"] = True
    else:
        # Starting fresh
        state["current_step"] = steps[0]
        state["steps"][steps[0]] = StepStatus.IN_PROGRESS.value

    write_change_state(change_path, state)
    return state


def compute_formality(change_path: Path, workflow: str) -> str:
    """Compute the formality level (large/medium/small)."""
    if workflow == "small":
        return "small"

    has_proposal = (change_path / "proposal.md").exists()
    has_specs = (change_path / "specs").exists() and any((change_path / "specs").iterdir())
    has_design = (change_path / "design.md").exists()
    tasks = read_tasks(change_path)

    if has_proposal and has_specs and has_design:
        return "large"
    elif has_proposal and has_specs:
        return "medium"
    elif not has_proposal and not has_specs and len(tasks) <= 3:
        return "small"

    return "medium"


def run_work(
    project_root: str = ".",
    change_name: Optional[str] = None,
    new_workflow: Optional[str] = None,
    advance: bool = False,
) -> Dict[str, Any]:
    """Run the work command and return JSON with actions."""
    root = Path(project_root).resolve()
    openspec_dir = root / "openspec" / "changes"

    # Handle --new flag (create new change)
    # Usage: se3 work <change-name> --new <workflow-type>
    #    or: se3 work --new <change-name> (workflow auto-detected from name)
    if new_workflow:
        # Determine change name and workflow type
        if change_name:
            # Format: se3 work <name> --new <workflow>
            actual_change_name = change_name
            actual_workflow = new_workflow
        else:
            # Format: se3 work --new <name>
            # Extract workflow from name prefix (e.g., "feature/auth" -> "feature")
            actual_change_name = new_workflow
            if "/" in actual_change_name:
                prefix = actual_change_name.split("/")[0]
                actual_workflow = prefix if prefix in WORKFLOWS else "feature"
            else:
                actual_workflow = "feature"

        # Create new change
        change_path = openspec_dir / actual_change_name
        change_path.mkdir(parents=True, exist_ok=True)

        # Initialize workflow state with explicit workflow type
        state = initialize_change_state(change_path, actual_change_name)
        # Override detected workflow if explicitly specified
        if actual_workflow in WORKFLOWS:
            state["workflow"] = actual_workflow
            state["steps"] = {step: StepStatus.PENDING.value for step in WORKFLOWS[actual_workflow]}
            state["current_step"] = WORKFLOWS[actual_workflow][0]
            write_change_state(change_path, state)

        return {
            "change": actual_change_name,
            "workflow": state["workflow"],
            "current_step": state["current_step"],
            "actions": compute_step_actions(
                actual_change_name, state["workflow"], state["current_step"], change_path
            ),
        }

    # List active changes if no change specified
    if not change_name:
        active_changes = []
        if openspec_dir.exists():
            # Recursively find all directories containing .se3-state.json
            for state_file in openspec_dir.rglob(".se3-state.json"):
                change_path = state_file.parent.relative_to(openspec_dir)
                active_changes.append(str(change_path))

        if active_changes:
            return {
                "change": None,
                "active_changes": active_changes,
                "actions": [
                    {
                        "type": "select_change",
                        "choices": active_changes,
                        "reason": "Multiple active changes - select one to work on",
                    }
                ],
            }
        else:
            return {
                "change": None,
                "active_changes": [],
                "actions": [
                    {
                        "type": "ask_user",
                        "question": "What do you want to work on? Describe the change (bug fix, feature, review, etc.)",
                        "reason": "No active changes - create new one",
                    }
                ],
            }

    # Work on existing change
    change_path = openspec_dir / change_name
    if not change_path.exists():
        return {
            "change": change_name,
            "error": f"Change '{change_name}' not found",
            "actions": [
                {
                    "type": "ask_user",
                    "question": f"Change '{change_name}' not found. Create it?",
                    "reason": "Change does not exist",
                }
            ],
        }

    # Read or initialize state
    state = read_change_state(change_path)
    if not state:
        state = initialize_change_state(change_path, change_name)

    # Handle --advance flag
    if advance:
        state = advance_step(state, change_path)

    # Compute current status
    workflow = state.get("workflow", "feature")
    current_step = state.get("current_step")
    steps = state.get("steps", {})
    step_list = WORKFLOWS.get(workflow, WORKFLOWS["feature"])

    steps_status = []
    for step in step_list:
        steps_status.append({
            "id": step,
            "status": steps.get(step, StepStatus.PENDING.value),
        })

    # Read tasks
    tasks = read_tasks(change_path)
    task_progress = {
        "total": len(tasks),
        "done": sum(1 for t in tasks if t.get("done", False)),
        "remaining": sum(1 for t in tasks if not t.get("done", False)),
    }

    # Compute formality
    formality = compute_formality(change_path, workflow)

    # Compute actions
    if current_step:
        actions = compute_step_actions(change_name, workflow, current_step, change_path)
    else:
        # All steps complete
        actions = [{"type": "complete", "reason": "All workflow steps complete"}]

    return {
        "change": change_name,
        "workflow": workflow,
        "formality": formality,
        "current_step": current_step,
        "steps": steps_status,
        "tasks": tasks,
        "progress": task_progress,
        "actions": actions,
    }


def print_text_report(result: Dict[str, Any]) -> None:
    """Print a human-readable work report."""
    if "error" in result:
        print(f"\nError: {result['error']}")
        print(f"Suggestion: {result.get('actions', [{}])[0].get('reason', '')}")
        return

    if not result.get("change"):
        # No change selected
        print(f"\n{'=' * 60}")
        print("SE 3.0 Work")
        print(f"{'=' * 60}")

        changes = result.get("active_changes", [])
        if changes:
            print(f"\nActive Changes:")
            for c in changes:
                print(f"  - {c}")
            print(f"\nRun: se3 work <change-name>")
        else:
            print(f"\nNo active changes.")
            print(f"Run: se3 work --new <type>/<name>")
        print(f"{'=' * 60}\n")
        return

    print(f"\n{'=' * 60}")
    print(f"SE 3.0 Work: {result['change']}")
    print(f"{'=' * 60}")

    print(f"\nWorkflow: {result['workflow']} ({result.get('formality', 'unknown')})")
    print(f"Current Step: {result.get('current_step', 'COMPLETE')}")

    # Steps
    steps = result.get("steps", [])
    if steps:
        print(f"\nSteps:")
        for step in steps:
            status_icon = {
                "done": "✓",
                "in_progress": "→",
                "pending": "○",
                "skipped": "⊘",
                "blocked": "✗",
            }.get(step["status"], "?")
            print(f"  {status_icon} {step['id']}: {step['status']}")

    # Tasks
    progress = result.get("progress", {})
    if progress.get("total", 0) > 0:
        print(f"\nTasks: {progress['done']}/{progress['total']} complete")

    # Actions
    actions = result.get("actions", [])
    if actions:
        print(f"\n{'-' * 60}")
        print("Next Actions:")
        print(f"{'-' * 60}")
        for i, action in enumerate(actions, 1):
            print(f"\n{i}. [{action['type']}] {action.get('reason', '')}")
            if 'description' in action:
                print(f"   {action['description']}")
            if 'cmd' in action:
                print(f"   Command: {action['cmd']}")

    print(f"\n{'=' * 60}\n")


def print_json_report(result: Dict[str, Any]) -> None:
    """Print JSON work report."""
    print(json.dumps(result, indent=2, default=str))


@app.command()
def work(
    change_name: Optional[str] = typer.Argument(None, help="Name of the change to work on"),
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
    new: Optional[str] = typer.Option(None, "--new", help="Create new change with workflow type (bugfix/feature/review/directive)"),
    advance: bool = typer.Option(False, "--advance", "-a", help="Mark current step complete and advance to next"),
    format: str = typer.Option("text", "--format", "-f", help="Output format (text or json)"),
):
    """Start or continue working on a change — the SDD workflow driver.

    Encodes all SDD workflows (bugfix, feature, review, directive) into
    programmatic logic with step tracking and adaptive formality.

    Examples:
        se3 work                    # List active changes
        se3 work feature/auth       # Continue working on change
        se3 work --new feature/auth # Create new change
        se3 work feature/auth -a    # Advance to next step
        se3 work feature/auth --json
    """
    result = run_work(project_root, change_name, new, advance)

    if format == "json":
        print_json_report(result)
    else:
        print_text_report(result)

    # Exit code: 0 = OK, 1 = no change or error
    raise typer.Exit(code=0 if result.get("change") else 1)
