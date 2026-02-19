"""SE3 Done command — the workflow driver for session shutdown.

Encodes the shutdown protocol into programmatic logic that returns
a JSON actions array for the agent to execute.

Steps encoded here:
1. Check for uncommitted changes
2. Check active changes status
3. Determine if tests should run
4. Compute the sequence of actions needed
"""

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import typer

app = typer.Typer(invoke_without_command=True)


def has_uncommitted_changes(project_root: Path) -> Tuple[bool, Dict[str, Any]]:
    """Check if there are uncommitted changes."""
    result = {"has_changes": False, "staged": 0, "unstaged": 0, "untracked": 0, "files": []}

    # Check staged changes
    staged_result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=project_root, capture_output=True, text=True
    )
    if staged_result.returncode == 0 and staged_result.stdout.strip():
        files = staged_result.stdout.strip().split("\n")
        result["staged"] = len(files)
        result["files"].extend(files)

    # Check unstaged changes
    unstaged_result = subprocess.run(
        ["git", "diff", "--name-only"],
        cwd=project_root, capture_output=True, text=True
    )
    if unstaged_result.returncode == 0 and unstaged_result.stdout.strip():
        files = unstaged_result.stdout.strip().split("\n")
        result["unstaged"] = len(files)
        result["files"].extend(files)

    # Check untracked files
    untracked_result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=project_root, capture_output=True, text=True
    )
    if untracked_result.returncode == 0 and untracked_result.stdout.strip():
        files = untracked_result.stdout.strip().split("\n")
        result["untracked"] = len(files)
        result["files"].extend(files)

    result["has_changes"] = result["staged"] > 0 or result["unstaged"] > 0 or result["untracked"] > 0
    result["files"] = list(set(result["files"]))  # Deduplicate
    result["count"] = len(result["files"])

    return result["has_changes"], result


def compute_active_changes(project_root: Path) -> List[Dict[str, Any]]:
    """Find active (non-archived) openspec changes with their status."""
    changes_dir = project_root / "openspec" / "changes"
    if not changes_dir.exists():
        return []

    changes = []
    # Recursively find all directories containing .se3-state.json
    for state_file in changes_dir.rglob(".se3-state.json"):
        change_path = state_file.parent
        change_name = str(change_path.relative_to(changes_dir))

        # Skip archived changes (paths starting with "archive/")
        if change_name.startswith("archive/"):
            continue

        # Read workflow state
        state = None
        try:
            state = json.loads(state_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

        change_info = {"name": change_name, "path": str(change_path)}

        if state:
            change_info["workflow"] = state.get("workflow", "unknown")
            change_info["current_step"] = state.get("current_step", "unknown")
            change_info["complete"] = state.get("complete", False)

            # Count remaining tasks if tasks.md exists
            tasks_file = change_path / "tasks.md"
            if tasks_file.exists():
                content = tasks_file.read_text()
                total = content.count("- [")
                done = content.count("- [x]")
                change_info["tasks"] = {"total": total, "done": done, "remaining": total - done}

        changes.append(change_info)

    return changes


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


def is_in_collab_mode(project_root: Path) -> bool:
    """Detect if we're running under se3 collab.

    Only returns True if SE3_AGENT_ROLE is set in the environment, which
    indicates this process was spawned by the collab orchestrator as a
    work agent. The existence of .collab/config.json only means a collab
    session exists, not that the current process is part of it.
    """
    return bool(os.environ.get("SE3_AGENT_ROLE"))


def cleanup_tmp_files(project_root: Path, max_age_days: int = 7) -> int:
    """Clean up temporary files in se3/tmp/ older than max_age_days.

    Also cleans legacy tmp*.prompt files in root directory.

    Returns number of files deleted.
    """
    from datetime import datetime, timedelta

    deleted_count = 0
    cutoff = datetime.now() - timedelta(days=max_age_days)

    # New location: se3/tmp/ (VISIBLE, not hidden)
    se3_tmp_dir = project_root / "se3" / "tmp"
    if se3_tmp_dir.exists():
        for tmp_file in se3_tmp_dir.glob("*"):
            if tmp_file.is_file():
                try:
                    mtime = datetime.fromtimestamp(tmp_file.stat().st_mtime)
                    if mtime < cutoff:
                        tmp_file.unlink()
                        deleted_count += 1
                except (OSError, PermissionError):
                    pass

    # Legacy location: root tmp*.prompt files
    for tmp_file in project_root.glob("tmp*.prompt"):
        try:
            tmp_file.unlink()
            deleted_count += 1
        except (OSError, PermissionError):
            pass

    return deleted_count


def verify_spec_scenarios(change_path: Path) -> List[Dict[str, Any]]:
    """Verify all spec scenarios pass for a change.

    Returns list of scenarios with pass/fail status.
    """
    scenarios = []
    specs_dir = change_path / "specs"

    if not specs_dir.exists():
        return scenarios

    # Find all spec files
    for spec_file in specs_dir.rglob("*.md"):
        content = spec_file.read_text()

        # Parse WHEN/THEN scenarios
        lines = content.split("\n")
        current_scenario = None

        for line in lines:
            line_stripped = line.strip()
            if line_stripped.startswith("WHEN"):
                current_scenario = {
                    "when": line_stripped,
                    "then": None,
                    "file": spec_file.name,
                }
            elif line_stripped.startswith("THEN") and current_scenario:
                current_scenario["then"] = line_stripped
                scenarios.append(current_scenario)
                current_scenario = None

    return scenarios


def compute_actions(state: Dict[str, Any], auto_archive: bool = False) -> List[Dict[str, Any]]:
    """Compute the shutdown actions array based on current state.

    Args:
        state: Current session state
        auto_archive: If True, automatically archive all completed changes
    """
    actions = []

    # Check for uncommitted changes
    uncommitted = state.get("uncommitted_changes", {})
    has_changes = uncommitted.get("has_changes", False)

    # Check for active changes
    active_changes = state.get("active_changes", [])
    incomplete_changes = [c for c in active_changes if not c.get("complete", False)]

    # Check for test command
    test_command = state.get("test_command")

    # Collab mode has different shutdown path
    if state.get("collab_mode"):
        actions.append({
            "type": "create_human_call",
            "reason": "Collab mode: create human-call for orchestrator",
        })
        return actions

    # Standard shutdown protocol

    # 0. Run health check first - OpenSpec integrity is foundational
    # This ensures the system is healthy before we proceed with shutdown
    from se3_tools.commands.health import run_health_check
    health_results = run_health_check(".", stale_days=30, include_archived=False, skip_test_changes=True)
    state["health_results"] = health_results

    # If there are health errors, add them as blocking actions
    if health_results.get("summary", {}).get("errors", 0) > 0:
        actions.append({
            "type": "health_check_errors",
            "severity": "error",
            "count": health_results["summary"]["errors"],
            "reason": f"OpenSpec integrity errors detected: {health_results['summary']['errors']} errors must be fixed",
            "cmd": "se3 health",
        })

    # If there are health warnings, surface them (non-blocking but visible)
    if health_results.get("summary", {}).get("warnings", 0) > 0:
        actions.append({
            "type": "health_check_warnings",
            "severity": "warning",
            "count": health_results["summary"]["warnings"],
            "reason": f"OpenSpec integrity warnings: {health_results['summary']['warnings']} warnings detected",
            "cmd": "se3 health",
        })

    # 1. Run tests (if test command exists and there are changes to verify)
    # Note: We defer actual test running to the agent, but flag if needed
    if test_command and has_changes:
        actions.append({
            "type": "run_tests",
            "cmd": test_command,
            "reason": "Must pass tests before commit",
        })

    # 1.5 Verify spec scenarios for incomplete changes (SE3 1.x feature)
    for change in incomplete_changes:
        if change.get("tasks", {}).get("remaining", 0) == 0:
            # All tasks done - verify scenarios before archiving
            actions.append({
                "type": "verify_scenarios",
                "change": change["name"],
                "reason": f"Verify all spec scenarios pass before archiving '{change['name']}'",
            })

    # 2. Commit changes
    if has_changes:
        files = uncommitted.get("files", [])
        actions.append({
            "type": "commit",
            "cmd": "se3 commit",
            "files": files,
            "reason": f"{uncommitted.get('count', 0)} uncommitted changes must be committed before handoff",
        })

    # 3. Update active changes status
    for change in incomplete_changes:
        remaining = change.get('tasks', {}).get('remaining', 0)
        if remaining == 0:
            # All tasks complete - archive the change (SE3 1.x feature)
            if auto_archive:
                # Auto-archive mode: archive immediately without prompting
                actions.append({
                    "type": "archive_change_immediate",
                    "change": change["name"],
                    "cmd": f"openspec archive {change['name']}",
                    "reason": f"Auto-archiving completed change '{change['name']}' (--archive flag set)",
                })
            else:
                # Normal mode: suggest archiving as an action
                actions.append({
                    "type": "archive_change",
                    "change": change["name"],
                    "cmd": f"openspec archive {change['name']}",
                    "reason": f"Archive completed change '{change['name']}'",
                })
            # Check for spec drift after archiving (SE3 1.x feature)
            actions.append({
                "type": "check_spec_drift",
                "change": change["name"],
                "reason": f"Check if specs were inappropriately weakened in '{change['name']}'",
            })
        else:
            actions.append({
                "type": "update_change_status",
                "change": change["name"],
                "note": f"{remaining} tasks remaining for next session",
                "reason": f"Document remaining work for '{change['name']}'",
            })

    # 4. Cleanup temporary files
    actions.append({
        "type": "cleanup_tmp",
        "reason": "Clean up temporary files older than 7 days",
    })

    # 6. Handoff / Finalize session
    actions.append({
        "type": "handoff",
        "cmd": "se3 handoff",
        "reason": "Generate session summary in progress.md and transfer control to human",
    })

    # 5. Clear session file
    actions.append({
        "type": "clear_session",
        "reason": "Mark session as ended",
    })

    return actions


def check_session_state(project_root: Path) -> Optional[Dict[str, Any]]:
    """Check if session is properly started.

    Returns None if session is valid, otherwise returns error dict with actions.
    """
    session_file = project_root / ".claude" / ".session.json"

    if not session_file.exists():
        return {
            "error": "SESSION_NOT_STARTED",
            "message": "No active session. Run 'se3 start' first.",
            "actions": [
                {
                    "type": "run_command",
                    "command": "se3 start --json",
                    "reason": "Session guard: Must start session before ending",
                }
            ],
        }

    try:
        session = json.loads(session_file.read_text())
        if session.get("status") != "active":
            return {
                "error": "SESSION_NOT_ACTIVE",
                "message": f"Session status is '{session.get('status')}'. Run 'se3 start' to resume.",
                "actions": [
                    {
                        "type": "run_command",
                        "command": "se3 start --json",
                        "reason": "Session guard: Must activate session before ending",
                    }
                ],
            }
    except (json.JSONDecodeError, OSError):
        return {
            "error": "SESSION_INVALID",
            "message": "Session file is corrupted. Run 'se3 start' to recreate.",
            "actions": [
                {
                    "type": "run_command",
                    "command": "se3 start --json",
                    "reason": "Session guard: Must recreate valid session",
                }
            ],
        }

    return None


def clear_session_file(project_root: Path) -> None:
    """Clear the session file to mark session as ended."""
    session_file = project_root / ".claude" / ".session.json"
    if session_file.exists():
        try:
            session_data = json.loads(session_file.read_text())
            session_data["status"] = "ended"
            session_data["ended_at"] = datetime.now().isoformat()
            session_file.write_text(json.dumps(session_data, indent=2), encoding="utf-8")
        except (json.JSONDecodeError, OSError):
            # If file is corrupted, just remove it
            session_file.unlink(missing_ok=True)


def run_session_done(project_root: str = ".", auto_archive: bool = False) -> Dict[str, Any]:
    """Run the session shutdown protocol and return JSON actions.

    This is the workflow driver — it computes the state and determines
    what actions the agent should take to properly end the session.

    Args:
        project_root: Root directory of the project
        auto_archive: If True, automatically archive all completed changes
    """
    root = Path(project_root).resolve()

    # Session Guard: Check session state before proceeding
    session_error = check_session_state(root)
    if session_error:
        return session_error

    # Compute state
    has_changes, uncommitted_info = has_uncommitted_changes(root)
    active_changes = compute_active_changes(root)
    test_command = detect_test_command(root)
    collab_mode = is_in_collab_mode(root)

    # Get git branch
    branch_result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=root, capture_output=True, text=True
    )
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "unknown"

    # Get recent commits
    commits_result = subprocess.run(
        ["git", "log", "--oneline", "-3"],
        cwd=root, capture_output=True, text=True
    )
    recent_commits = commits_result.stdout.strip().split("\n") if commits_result.returncode == 0 else []

    state = {
        "project_root": str(root),
        "branch": branch,
        "uncommitted_changes": uncommitted_info,
        "active_changes": active_changes,
        "test_command": test_command,
        "collab_mode": collab_mode,
        "recent_commits": recent_commits,
        "auto_archive": auto_archive,
    }

    # Compute actions
    state["actions"] = compute_actions(state, auto_archive=auto_archive)

    return state


def print_text_report(state: Dict[str, Any]) -> None:
    """Print a human-readable session done report."""
    print(f"\n{'=' * 60}")
    print("SE 3.0 Session Done")
    print(f"{'=' * 60}")

    # Git info
    print(f"\nBranch: {state.get('branch', 'N/A')}")

    # OpenSpec Health Check (now automatic)
    health = state.get("health_results", {})
    if health:
        summary = health.get("summary", {})
        stats = health.get("stats", {})
        is_healthy = health.get("healthy", False)

        print(f"\nOpenSpec Health:")
        if is_healthy and summary.get("warnings", 0) == 0:
            print(f"  Status: HEALTHY ✓")
        elif summary.get("errors", 0) > 0:
            print(f"  Status: ERRORS DETECTED ✗")
        else:
            print(f"  Status: OK (with info)")

        print(f"  Changes: {stats.get('active_changes', 0)} active, {stats.get('archived_changes', 0)} archived")
        print(f"  Issues: {summary.get('errors', 0)} errors, {summary.get('warnings', 0)} warnings, {summary.get('info', 0)} info")

        # Show top health issues if any
        issues = health.get("issues", [])
        if issues:
            print(f"  Notable issues:")
            for issue in issues[:3]:  # Show first 3
                severity = issue.get("severity", "info")
                category = issue.get("category", "unknown")
                message = issue.get("message", "")
                icon = "✗" if severity == "error" else "!" if severity == "warning" else "i"
                print(f"    [{icon}] [{category}] {message}")
            if len(issues) > 3:
                print(f"    ... and {len(issues) - 3} more (run 'se3 health' for details)")

    # Uncommitted changes
    uncommitted = state.get("uncommitted_changes", {})
    if uncommitted.get("has_changes"):
        print(f"\nUncommitted Changes: {uncommitted.get('count', 0)}")
        if uncommitted.get("staged"):
            print(f"  - Staged: {uncommitted['staged']} files")
        if uncommitted.get("unstaged"):
            print(f"  - Unstaged: {uncommitted['unstaged']} files")
        if uncommitted.get("untracked"):
            print(f"  - Untracked: {uncommitted['untracked']} files")
    else:
        print(f"\nWorking Tree: clean")

    # Active changes
    changes = state.get("active_changes", [])
    if changes:
        print(f"\nActive Changes:")
        for c in changes:
            status = "complete" if c.get("complete") else f"step: {c.get('current_step', 'unknown')}"
            tasks = c.get("tasks", {})
            if tasks:
                status += f", tasks: {tasks.get('done', 0)}/{tasks.get('total', 0)}"
            print(f"  - {c['name']}: {status}")

    # Actions
    actions = state.get("actions", [])
    if actions:
        print(f"\n{'-' * 60}")
        print("Shutdown Actions:")
        print(f"{'-' * 60}")
        for i, action in enumerate(actions, 1):
            print(f"\n{i}. [{action['type']}] {action.get('reason', '')}")
            if 'cmd' in action:
                print(f"   Command: {action['cmd']}")
            if 'files' in action and isinstance(action['files'], list):
                print(f"   Files: {', '.join(action['files'][:5])}")
                if len(action['files']) > 5:
                    print(f"   ... and {len(action['files']) - 5} more")

    print(f"\n{'=' * 60}\n")


def print_json_report(state: Dict[str, Any]) -> None:
    """Print JSON session done report."""
    print(json.dumps(state, indent=2, default=str))


@app.callback()
def done(
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
    format: str = typer.Option("text", "--format", "-f", help="Output format (text or json)"),
    archive: bool = typer.Option(False, "--archive", "-a", help="Automatically archive all completed changes"),
):
    """End an SE3 session — compute shutdown actions for the agent.

    Encodes the shutdown protocol into programmatic logic, returning a
    JSON actions array that tells the agent exactly what to do:
    - Run tests (if needed)
    - Commit changes (if uncommitted)
    - Update change status
    - Archive completed changes (with --archive flag)
    - Handoff to human

    Examples:
        se3 done
        se3 done --json
        se3 done -p /path/to/project --json
        se3 done --archive       # Auto-archive completed changes
    """
    state = run_session_done(project_root, auto_archive=archive)

    if format == "json":
        print_json_report(state)
    else:
        print_text_report(state)

    # Exit code: 0 = clean, 1 = actions needed
    raise typer.Exit(code=0 if not state.get("actions") else 1)
