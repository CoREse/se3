"""SE3 Start command — the workflow driver for session initialization.

Encodes the 7-step startup protocol into programmatic logic that returns
a JSON actions array for the agent to execute.

Steps encoded here:
1. Environment setup (check init.sh)
2. Specs directory check (specs/ exists)
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


def create_session_branch(project_root: Path) -> str:
    """Create a new session branch with se3-session/<timestamp> pattern.

    Returns the name of the created branch.
    """
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    branch_name = f"se3-session/{timestamp}"

    # Create and checkout the new branch
    result = subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=project_root, capture_output=True, text=True
    )

    if result.returncode != 0:
        # If branch creation fails, return current branch
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=project_root, capture_output=True, text=True
        )
        return result.stdout.strip() or "unknown"

    return branch_name


def compute_git_status(project_root: Path, create_branch: bool = True) -> Dict[str, Any]:
    """Compute current git state.

    Args:
        project_root: Root directory of the project
        create_branch: If True, create a new se3-session branch
    """
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

    # Create new session branch if requested and we're not already on a se3-session branch
    if create_branch and not info["branch"].startswith("se3-session/"):
        new_branch = create_session_branch(project_root)
        info["branch"] = new_branch
        info["branch_created"] = True
    elif info["branch"].startswith("se3-session/"):
        info["branch_created"] = False
        info["branch_reused"] = True

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


def check_specs(project_root: Path) -> Dict[str, Any]:
    """Check if SE3 specs directory exists and is initialized.

    Checks for se3/specs/ (preferred), specs/ (fallback), or openspec/specs/ (legacy).
    """
    specs_dir = project_root / "se3" / "specs"
    fallback_dir = project_root / "specs"
    legacy_dir = project_root / "openspec" / "specs"

    if specs_dir.exists():
        spec_count = len(list(specs_dir.glob("*/spec.md")))
        return {
            "initialized": True,
            "path": str(specs_dir),
            "spec_count": spec_count,
            "using_fallback": False,
        }
    elif fallback_dir.exists():
        spec_count = len(list(fallback_dir.glob("*/spec.md")))
        return {
            "initialized": True,
            "path": str(fallback_dir),
            "spec_count": spec_count,
            "using_fallback": True,
        }
    elif legacy_dir.exists():
        spec_count = len(list(legacy_dir.glob("*/spec.md")))
        return {
            "initialized": True,
            "path": str(legacy_dir),
            "spec_count": spec_count,
            "using_fallback": True,
        }
    else:
        return {
            "initialized": False,
            "path": None,
            "spec_count": 0,
            "using_fallback": False,
        }


def compute_active_changes(project_root: Path) -> List[str]:
    """Find active (non-archived) changes.

    Checks openspec/changes/ for legacy state files.
    """
    changes_dir = project_root / "openspec" / "changes"
    if not changes_dir.exists():
        return []

    active = []
    # Recursively find all directories containing .se3-state.json
    for state_file in changes_dir.rglob(".se3-state.json"):
        # Get relative path from changes_dir, remove .se3-state.json filename
        change_path = state_file.parent.relative_to(changes_dir)
        change_name = str(change_path)
        # Skip archived changes (paths starting with "archive/")
        if change_name.startswith("archive/"):
            continue
        active.append(change_name)

    return sorted(active)


def check_pending_human_calls(project_root: Path) -> List[Dict[str, Any]]:
    """Check for responded human calls that need processing.

    Checks new se3/calls/active/ location first, falls back to legacy human-calls/.
    """
    # New location (SE3 2.x+, VISIBLE directory)
    calls_dir = project_root / "se3" / "calls" / "active"
    if not calls_dir.exists():
        # Legacy hidden .se3/ (earlier 2.x)
        calls_dir = project_root / ".se3" / "calls" / "active"
        if not calls_dir.exists():
            # Legacy location (pre-2.x)
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
    """Compute collaboration session status.

    Checks new se3/collab/ location first, falls back to legacy .collab/.
    """
    # New location (SE3 2.x+, VISIBLE directory)
    collab_dir = project_root / "se3" / "collab"
    config_file = collab_dir / "config.json"

    if not config_file.exists():
        # Legacy hidden .se3/ (earlier 2.x)
        collab_dir = project_root / ".se3" / "collab"
        config_file = collab_dir / "config.json"
        if not config_file.exists():
            # Legacy location (pre-2.x)
            collab_dir = project_root / ".collab"
            config_file = collab_dir / "config.json"
            if not config_file.exists():
                return None

    try:
        config = json.loads(config_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {"status": "error", "message": "Cannot read collab config"}

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


def classify_input(user_message: str) -> str:
    """Classify user input intent type.

    Returns one of: directive, bug-report, feature-request, question,
    review, clarification, meta, off-topic
    """
    message_lower = user_message.lower()

    # Bug report indicators
    bug_indicators = ["error", "bug", "broken", "fail", "crash", "exception",
                      "stack trace", "not working", "doesn't work"]
    if any(ind in message_lower for ind in bug_indicators):
        return "bug-report"

    # Review indicators
    review_indicators = ["review", "check this", "look at", "what do you think",
                         "is this correct", "evaluate"]
    if any(ind in message_lower for ind in review_indicators):
        return "review"

    # Feature request indicators
    feature_indicators = ["add ", "implement", "create ", "build ", "support ",
                          "feature", "new capability", "enhancement"]
    if any(ind in message_lower for ind in feature_indicators):
        return "feature-request"

    # Question indicators
    question_indicators = ["how ", "why ", "what is", "explain", "?"]
    if any(ind in message_lower for ind in question_indicators):
        return "question"

    # Directive indicators (explicit commands)
    directive_indicators = ["self-iterate", "continue", "proceed", "start ",
                            "fix ", "update ", "refactor "]
    if any(ind in message_lower for ind in directive_indicators):
        return "directive"

    # Clarification / continuation
    clarification_indicators = ["also", "additionally", "and", "then", "next"]
    if any(ind in message_lower for ind in clarification_indicators):
        return "clarification"

    # Meta indicators (about the project/process itself)
    meta_indicators = ["what is se3", "how does se3 work", "explain se3", "about the process",
                       "about the framework", "se3 workflow", "project structure"]
    if any(ind in message_lower for ind in meta_indicators):
        return "meta"

    # Off-topic indicators (not related to project)
    off_topic_indicators = ["what's the weather", "tell me a joke", "who are you",
                            "hello", "hi there", "good morning", "good evening"]
    if any(ind in message_lower for ind in off_topic_indicators):
        return "off-topic"

    # Default to directive for most inputs
    return "directive"


def determine_stage(intent: str, current_state: Dict[str, Any]) -> Dict[str, Any]:
    """Determine workflow stage based on input intent and current state.

    Returns stage decision with recommended workflow and actions.
    """
    active_changes = current_state.get("active_changes", [])

    if intent == "bug-report":
        # Check if related to active change
        if active_changes:
            return {
                "stage": "continue_change",
                "workflow": "bugfix",
                "note": "Adding bug fix to existing change context",
            }
        return {
            "stage": "new_change",
            "workflow": "bugfix",
            "note": "Creating new bugfix change",
        }

    elif intent == "feature-request":
        return {
            "stage": "new_change",
            "workflow": "feature",
            "note": "Creating new feature change",
        }

    elif intent == "review":
        return {
            "stage": "review",
            "workflow": "review",
            "note": "Starting review workflow",
        }

    elif intent == "question":
        return {
            "stage": "answer",
            "workflow": None,
            "note": "Answer directly without creating change",
        }

    elif intent == "directive":
        return {
            "stage": "execute",
            "workflow": "directive",
            "note": "Execute directive workflow",
        }

    elif intent == "clarification":
        return {
            "stage": "continue",
            "workflow": None,
            "note": "Continue previous context",
        }

    elif intent == "meta":
        return {
            "stage": "answer",
            "workflow": None,
            "note": "Answer meta question about SE3 process",
        }

    elif intent == "off-topic":
        return {
            "stage": "answer",
            "workflow": None,
            "note": "Answer without modifying project files",
        }

    # Default
    return {
        "stage": "execute",
        "workflow": "directive",
        "note": "Default to directive execution",
    }


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
            "type": "create_se3_dirs",
            "reason": "Initialize se3/ directory structure"
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

    # Specs directory check
    specs_info = state.get("specs", {})
    if not specs_info.get("initialized"):
        actions.append({
            "type": "init_specs",
            "cmd": "mkdir -p se3/specs",
            "reason": "se3/specs/ directory missing, needs initialization"
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


def run_session_start(project_root: str = ".", user_input: Optional[str] = None) -> Dict[str, Any]:
    """Run the full session start protocol and return JSON actions.

    This is the main workflow driver — it computes all state and determines
    what actions the agent should take.

    Args:
        project_root: Root directory of the project
        user_input: The user's first message (for intent classification)
    """
    root = Path(project_root).resolve()

    # First-time detection
    is_first_time = is_first_time_project(root)

    # Compute all state
    git_info = compute_git_status(root, create_branch=True)
    env_setup = check_init_script(root)
    specs_info = check_specs(root)
    active_changes = compute_active_changes(root)
    pending_calls = check_pending_human_calls(root)
    collab = compute_collab_status(root)
    progress_summary = read_progress_summary(root)
    test_command = detect_test_command(root)

    # Input Classification & Stage Routing (SE3 1.x feature)
    user_intent = classify_input(user_input) if user_input else "directive"

    # Build intermediate state for stage decision
    intermediate_state = {
        "active_changes": active_changes,
        "pending_human_calls": pending_calls,
    }
    stage_decision = determine_stage(user_intent, intermediate_state)

    # Determine if tests should be run
    test_baseline_needed = (
        git_info["uncommitted_count"] > 0 and  # Has uncommitted changes
        test_command is not None
    )

    # Build state dict
    state = {
        "project_root": str(root),
        "first_time": is_first_time,
        "env_setup": env_setup,
        "specs": specs_info,
        "git": {
            "branch": git_info["branch"],
            "uncommitted_count": git_info["uncommitted_count"],
            "last_commits": git_info["last_commits"],
            "branch_created": git_info.get("branch_created", False),
            "branch_reused": git_info.get("branch_reused", False),
        },
        "progress_summary": progress_summary,
        "active_changes": active_changes,
        "pending_human_calls": pending_calls,
        "collab": collab,
        "test_command": test_command,
        "test_baseline_needed": test_baseline_needed,
        "user_intent": user_intent,
        "stage_decision": stage_decision,
    }

    # Compute actions
    state["actions"] = compute_actions(state)

    # Add intent-based routing actions (SE3 1.x feature)
    if not is_first_time and user_input:
        intent_actions = compute_intent_actions(state)
        state["actions"].extend(intent_actions)

    # Create session file to mark session as started
    create_session_file(root)

    return state


def compute_intent_actions(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Compute actions based on detected user intent.

    This implements the Input Classification & Stage Routing from SE3 1.x.
    """
    actions = []
    intent = state.get("user_intent", "directive")
    stage = state.get("stage_decision", {})

    if intent == "bug-report":
        actions.append({
            "type": "route_to_bugfix",
            "workflow": "bugfix",
            "reason": "Bug report detected - route to bug fix workflow",
        })

    elif intent == "feature-request":
        actions.append({
            "type": "route_to_feature",
            "workflow": "feature",
            "reason": "Feature request detected - route to feature workflow",
        })

    elif intent == "review":
        actions.append({
            "type": "route_to_review",
            "workflow": "review",
            "reason": "Review request detected - route to review workflow",
        })

    elif intent == "question":
        actions.append({
            "type": "explore_and_answer",
            "reason": "Question detected - explore code and provide answer",
        })

    elif intent == "directive":
        actions.append({
            "type": "execute_directive",
            "workflow": stage.get("workflow", "directive"),
            "reason": "Directive detected - execute with SDD workflow",
        })

    return actions


def create_session_file(project_root: Path) -> None:
    """Create .session.json to mark session as active."""
    claude_dir = project_root / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    session_file = claude_dir / ".session.json"
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

    # Input Classification (SE3 1.x feature)
    user_intent = state.get("user_intent")
    if user_intent:
        stage = state.get("stage_decision", {})
        print(f"\nIntent: {user_intent}")
        print(f"Stage: {stage.get('stage', 'N/A')}")
        if stage.get('workflow'):
            print(f"Workflow: {stage.get('workflow')}")

    # Git info
    git = state.get("git", {})
    print(f"\nBranch: {git.get('branch', 'N/A')}")
    uncommitted = git.get('uncommitted_count', 0)
    if uncommitted > 0:
        print(f"Uncommitted Changes: {uncommitted}")
    else:
        print(f"Working Tree: clean")

    # Specs
    specs_info = state.get("specs", {})
    if specs_info.get("initialized"):
        print(f"\nSpecs: {specs_info.get('spec_count', 0)} specs in {specs_info.get('path', 'specs/')}")
    else:
        print(f"\nSpecs: Not initialized (no specs/ directory)")

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
    input: Optional[str] = typer.Option(None, "--input", "-i", help="User input for intent classification"),
):
    """Start an SE3 session — compute state and return actions for the agent.

    This is the workflow driver for session initialization. It encodes the
    7-step startup protocol into programmatic logic, returning a JSON actions
    array that tells the agent exactly what to do next.

    Includes Input Classification & Stage Routing (SE3 1.x feature).

    Examples:
        se3 start
        se3 start --json
        se3 start -p /path/to/project --json
        se3 start -i "Fix the login bug"
    """
    state = run_session_start(project_root, input)

    if format == "json":
        print_json_report(state)
    else:
        print_text_report(state)

    # Exit code: 0 = ready, 1 = actions needed
    raise typer.Exit(code=0 if not state.get("actions") else 1)
