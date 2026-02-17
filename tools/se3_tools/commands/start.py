"""SE3 Start command — the workflow driver for session initialization.

Encodes the 7-step startup protocol into programmatic logic that returns
a JSON actions array for the agent to execute.

Steps encoded here:
1. Environment setup (check init.sh)
2. OpenSpec check (command available, directory exists)
3. Status check (git, collab, human-calls)
4. Load context (progress.md, git log)
5. Check pending items (responded human calls, active changes)
6. Baseline verification (tests needed?)
7. Compute actions for agent to execute
"""

import json
import subprocess
import os
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

from ..human_calls import HumanCallStore
import typer

app = typer.Typer(invoke_without_command=True)


def compute_git_status(project_root: Path) -> Dict[str, Any]:
    """Compute current git state."""
    info = {
        "branch": "unknown",
        "uncommitted_count": 0,
        "uncommitted_details": "",
        "last_commits": [],
    }

    # Branch
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=project_root, capture_output=True, text=True
    )
    if result.returncode == 0:
        info["branch"] = result.stdout.strip() or "(detached HEAD)"

    # Uncommitted changes
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_root, capture_output=True, text=True
    )
    if result.returncode == 0 and result.stdout.strip():
        lines = result.stdout.strip().split("\n")
        info["uncommitted_count"] = len(lines)
        info["uncommitted_details"] = result.stdout.strip()

    # Recent commits
    result = subprocess.run(
        ["git", "log", "--oneline", "-5"],
        cwd=project_root, capture_output=True, text=True
    )
    if result.returncode == 0 and result.stdout.strip():
        info["last_commits"] = result.stdout.strip().split("\n")

    return info


def check_init_script(project_root: Path) -> Dict[str, Any]:
    """Check if init.sh exists and needs to be run."""
    init_script = project_root / "init.sh"
    if not init_script.exists():
        return {"needed": False, "script": None, "exists": False}

    # Check if init.sh has been run (simple heuristic: check if required services are running)
    # For now, we just report that it exists and needs to be run
    return {"needed": True, "script": str(init_script), "exists": True}


def check_openspec(project_root: Path) -> Dict[str, Any]:
    """Check if openspec command is available and openspec/ directory exists."""
    result = subprocess.run(
        ["which", "openspec"],
        capture_output=True, text=True
    )
    available = result.returncode == 0

    openspec_dir = project_root / "openspec"
    initialized = openspec_dir.exists()

    return {
        "available": available,
        "initialized": initialized,
        "cmd": "openspec" if available else None,
    }


def compute_active_changes(project_root: Path) -> List[str]:
    """Find active (non-archived) openspec changes."""
    changes_dir = project_root / "openspec" / "changes"
    if not changes_dir.exists():
        return []

    active = []
    # Recursively find all directories containing .se3-state.json
    for state_file in changes_dir.rglob(".se3-state.json"):
        # Get relative path from changes_dir, remove .se3-state.json filename
        change_path = state_file.parent.relative_to(changes_dir)
        active.append(str(change_path))

    return sorted(active)


def check_pending_human_calls(project_root: Path) -> List[Dict[str, Any]]:
    """Check for responded human calls that need processing."""
    calls_dir = project_root / "human-calls"
    if not calls_dir.exists():
        return []

    store = HumanCallStore(calls_dir)
    responded = store.get_responded_calls()

    pending = []
    for call in responded:
        is_valid, reason = store.validate_response(call)
        if is_valid:
            pending.append({
                "file": call.file_path.name,
                "type": call.call_type.value,
                "title": call.title or "Untitled",
                "created": call.created.isoformat() if call.created else None,
            })

    return pending


def compute_collab_status(project_root: Path) -> Optional[Dict[str, Any]]:
    """Compute collaboration session status."""
    collab_dir = project_root / ".collab"
    config_file = collab_dir / "config.json"

    if not config_file.exists():
        return None

    try:
        config = json.loads(config_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {"status": "error", "message": "Cannot read .collab/config.json"}

    return {
        "status": config.get("status", "unknown"),
        "objective": config.get("objective", ""),
        "session_id": config.get("session_id", ""),
    }


def read_progress_summary(project_root: Path, num_entries: int = 3) -> List[str]:
    """Read recent progress entries from progress.md."""
    progress_file = project_root / "progress.md"
    if not progress_file.exists():
        return []

    try:
        content = progress_file.read_text()
        # Split by session headers (## YYYY-MM-DD)
        sessions = []
        current_session = []

        for line in content.split("\n"):
            if line.startswith("## ") and "Session" in line:
                if current_session:
                    sessions.append("\n".join(current_session))
                    current_session = []
            current_session.append(line)

        if current_session:
            sessions.append("\n".join(current_session))

        # Return most recent sessions (first in file)
        return sessions[:num_entries]
    except Exception:
        return []


def detect_test_command(project_root: Path) -> Optional[str]:
    """Detect the test command to use for baseline verification."""
    # Check for common test files/configs
    if (project_root / "pytest.ini").exists() or (project_root / "pyproject.toml").exists():
        if (project_root / "pyproject.toml").exists():
            return "python -m pytest tests/ -q"
        return "python -m pytest tests/ -q"

    if (project_root / "package.json").exists():
        return "npm test"

    if (project_root / "Cargo.toml").exists():
        return "cargo test"

    if (project_root / "go.mod").exists():
        return "go test ./..."

    # Check for tests directory
    if (project_root / "tests").exists():
        return "python -m pytest tests/ -q"

    return None


def is_first_time_project(project_root: Path) -> bool:
    """Check if this is a first-time project (empty or minimal)."""
    # No progress.md and no git commits = first time
    progress_file = project_root / "progress.md"
    if progress_file.exists():
        return False

    # Check if there are any commits
    result = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=project_root, capture_output=True, text=True
    )
    if result.returncode == 0 and result.stdout.strip():
        return False

    return True


def compute_actions(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Compute the actions array based on current state.

    This is the workflow driver — it determines what the agent should do next.
    """
    actions = []

    # First-time bootstrap
    if state.get("first_time"):
        actions.append({
            "type": "ask_user",
            "question": "What should this project do? Describe what you want to build.",
            "reason": "First-time project bootstrap"
        })
        actions.append({
            "type": "create_progress",
            "reason": "Initialize progress.md for cross-session history"
        })
        actions.append({
            "type": "create_human_calls_dir",
            "reason": "Initialize human-calls/ directory"
        })
        return actions

    # Environment setup
    env_setup = state.get("env_setup", {})
    if env_setup.get("needed"):
        actions.append({
            "type": "run_script",
            "cmd": f"bash {env_setup.get('script')}",
            "reason": "Environment setup required (init.sh found)"
        })

    # OpenSpec initialization
    openspec = state.get("openspec", {})
    if not openspec.get("available"):
        actions.append({
            "type": "ask_user",
            "question": "openspec command not found. Please install it: pip install openspec. Shall I proceed without openspec?",
            "reason": "openspec not installed"
        })
    elif not openspec.get("initialized"):
        actions.append({
            "type": "init_openspec",
            "cmd": "openspec init",
            "reason": "openspec/ directory missing, needs initialization"
        })

    # Baseline verification
    test_command = state.get("test_command")
    if test_command and state.get("test_baseline_needed"):
        actions.append({
            "type": "run_tests",
            "cmd": test_command,
            "reason": "Establish baseline before making changes (project has uncommitted changes)"
        })

    # Process pending human calls
    for call in state.get("pending_human_calls", []):
        actions.append({
            "type": "process_human_call",
            "file": call.get("file"),
            "reason": f"Unprocessed human response: {call.get('title', 'Untitled')}"
        })

    return actions


def run_session_start(project_root: str = ".") -> Dict[str, Any]:
    """Run the full session start protocol and return JSON actions.

    This is the main workflow driver — it computes all state and determines
    what actions the agent should take.
    """
    root = Path(project_root).resolve()

    # First-time detection
    is_first_time = is_first_time_project(root)

    # Compute all state
    git_info = compute_git_status(root)
    env_setup = check_init_script(root)
    openspec = check_openspec(root)
    active_changes = compute_active_changes(root)
    pending_calls = check_pending_human_calls(root)
    collab = compute_collab_status(root)
    progress_summary = read_progress_summary(root)
    test_command = detect_test_command(root)

    # Determine if tests should be run
    test_baseline_needed = (
        git_info["uncommitted_count"] == 0 and  # Clean workspace
        test_command is not None
    )

    # Build state dict
    state = {
        "project_root": str(root),
        "first_time": is_first_time,
        "env_setup": env_setup,
        "openspec": openspec,
        "git": {
            "branch": git_info["branch"],
            "uncommitted_count": git_info["uncommitted_count"],
            "last_commits": git_info["last_commits"],
        },
        "progress_summary": progress_summary,
        "active_changes": active_changes,
        "pending_human_calls": pending_calls,
        "collab": collab,
        "test_command": test_command,
        "test_baseline_needed": test_baseline_needed,
    }

    # Compute actions
    state["actions"] = compute_actions(state)

    # Create session file to mark session as started
    create_session_file(root)

    return state


def create_session_file(project_root: Path) -> None:
    """Create .session.json to mark session as active."""
    session_file = project_root / ".claude" / ".session.json"
    session_data = {
        "status": "active",
        "started_at": datetime.now().isoformat(),
        "pid": os.getpid(),
    }
    session_file.write_text(json.dumps(session_data, indent=2), encoding="utf-8")


def print_text_report(state: Dict[str, Any]) -> None:
    """Print a human-readable session start report."""
    print(f"\n{'=' * 60}")
    print("SE 3.0 Session Start")
    print(f"{'=' * 60}")

    # Git info
    git = state.get("git", {})
    print(f"\nBranch: {git.get('branch', 'N/A')}")
    uncommitted = git.get('uncommitted_count', 0)
    if uncommitted > 0:
        print(f"Uncommitted Changes: {uncommitted}")
    else:
        print(f"Working Tree: clean")

    # OpenSpec
    openspec = state.get("openspec", {})
    print(f"\nOpenSpec: {'Available' if openspec.get('available') else 'Not installed'}")
    print(f"OpenSpec Dir: {'Initialized' if openspec.get('initialized') else 'Missing'}")

    # Active changes
    changes = state.get("active_changes", [])
    if changes:
        print(f"\nActive Changes:")
        for c in changes:
            print(f"  - {c}")
    else:
        print(f"\nActive Changes: (none)")

    # Pending human calls
    calls = state.get("pending_human_calls", [])
    if calls:
        print(f"\nPending Human Calls:")
        for call in calls:
            print(f"  - {call['file']}: {call.get('title', 'Untitled')}")

    # Actions
    actions = state.get("actions", [])
    if actions:
        print(f"\n{'-' * 60}")
        print("Recommended Actions:")
        print(f"{'-' * 60}")
        for i, action in enumerate(actions, 1):
            print(f"\n{i}. [{action['type']}] {action.get('reason', '')}")
            if 'cmd' in action:
                print(f"   Command: {action['cmd']}")
            if 'question' in action:
                print(f"   Ask: {action['question']}")
    else:
        print(f"\n{'-' * 60}")
        print("No actions required — ready to work.")

    print(f"\n{'=' * 60}\n")


def print_json_report(state: Dict[str, Any]) -> None:
    """Print JSON session start report."""
    print(json.dumps(state, indent=2, default=str))


@app.callback()
def start(
    format: str = typer.Option("text", "--format", "-f", help="Output format (text or json)"),
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
):
    """Start an SE3 session — compute state and return actions for the agent.

    This is the workflow driver for session initialization. It encodes the
    7-step startup protocol into programmatic logic, returning a JSON actions
    array that tells the agent exactly what to do next.

    Examples:
        se3 start
        se3 start --json
        se3 start -p /path/to/project --json
    """
    state = run_session_start(project_root)

    if format == "json":
        print_json_report(state)
    else:
        print_text_report(state)

    # Exit code: 0 = ready, 1 = actions needed
    raise typer.Exit(code=0 if not state.get("actions") else 1)
