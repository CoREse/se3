"""Status diagnostics command for SE 3.0 tools.

Computes project status in real-time from:
- git status / git log (uncommitted changes, recent activity)
- openspec/changes/ (active changes)
- .collab/ (collaboration session state)
- human-calls/ (pending/responded requests)

No longer depends on status.md — all state is computed live.
"""

# Verify: status-diagnostics/Compute git status
# Verify: status-diagnostics/Detect active changes
# Verify: status-diagnostics/Unprocessed response
# Verify: status-diagnostics/Long-pending call
# Verify: status-diagnostics/Healthy project
# Verify: status-diagnostics/Issues found

import json
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

from ..utils import discover_changes
from ..human_calls import HumanCallStore

import typer

app = typer.Typer(invoke_without_command=True)


def compute_git_status(project_root: Path) -> Dict[str, Any]:
    """Compute current git state.

    Returns dict with branch, uncommitted changes count, last commit info.
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


def compute_active_changes(project_root: Path) -> List[str]:
    """Find active (non-archived) openspec changes."""
    changes_dir = project_root / "openspec" / "changes"
    if not changes_dir.exists():
        return []

    active = []
    for item in changes_dir.iterdir():
        if item.is_dir() and item.name != "archive":
            active.append(item.name)
    return sorted(active)


def compute_collab_status(project_root: Path) -> Optional[Dict[str, Any]]:
    """Compute collaboration session status from .collab/ files."""
    collab_dir = project_root / ".collab"
    config_file = collab_dir / "config.json"

    if not config_file.exists():
        return None

    try:
        config = json.loads(config_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {"status": "error", "message": "Cannot read .collab/config.json"}

    result = {
        "status": config.get("status", "unknown"),
        "objective": config.get("objective", ""),
        "session_id": config.get("session_id", ""),
        "tasks": [],
    }

    tasks_dir = collab_dir / "tasks"
    if tasks_dir.exists():
        for tf in sorted(tasks_dir.glob("task-*.json")):
            try:
                task = json.loads(tf.read_text())
                result["tasks"].append({
                    "id": task.get("id", tf.stem),
                    "status": task.get("status", "unknown"),
                    "title": task.get("title", ""),
                })
            except (json.JSONDecodeError, OSError):
                continue

    return result


def check_human_calls(project_root: Path, timeout_days: int = 7) -> List[Dict[str, Any]]:
    """Check human-calls directory for pending/responded files."""
    issues = []
    calls_dir = project_root / "human-calls"

    if not calls_dir.exists():
        return issues

    store = HumanCallStore(calls_dir)

    # Stale calls
    stale_calls = store.get_stale_calls(timeout_days)
    for call in stale_calls:
        issues.append({
            'severity': 'warning',
            'check': 'human_calls',
            'message': f"Long-pending human call: {call.file_path.name} ({call.created.strftime('%Y-%m-%d')})",
            'suggestion': "Follow up on the pending request or close it"
        })

    # Fresh pending
    all_pending = store.get_pending_calls()
    fresh_pending = [c for c in all_pending if c not in stale_calls]
    for call in fresh_pending:
        issues.append({
            'severity': 'info',
            'check': 'human_calls',
            'message': f"Pending human call: {call.file_path.name}",
            'suggestion': "Awaiting human response"
        })

    # Responded
    responded_calls = store.get_responded_calls()
    for call in responded_calls:
        is_valid, reason = store.validate_response(call)
        if is_valid:
            issues.append({
                'severity': 'warning',
                'check': 'human_calls',
                'message': f"Unprocessed response: {call.file_path.name}",
                'suggestion': "Process the response and update the call status"
            })
        else:
            issues.append({
                'severity': 'info',
                'check': 'human_calls',
                'message': f"Incomplete response in {call.file_path.name}: {reason}",
                'suggestion': "Wait for human to complete their response"
            })

    return issues


def run_diagnostics(project_root: str = ".") -> Dict[str, Any]:
    """Run all diagnostics by computing live state.

    Returns dict with computed status and diagnostic issues.
    """
    root = Path(project_root).resolve()
    issues = []

    # Compute live state
    git_info = compute_git_status(root)
    active_changes = compute_active_changes(root)
    collab = compute_collab_status(root)

    # Build computed status
    status = {
        "branch": git_info["branch"],
        "uncommitted_changes": git_info["uncommitted_count"],
        "active_changes": active_changes,
        "collab": collab,
        "last_commits": git_info["last_commits"],
    }

    # Diagnostics: uncommitted changes
    if git_info["uncommitted_count"] > 0:
        issues.append({
            'severity': 'info',
            'check': 'git_status',
            'message': f"{git_info['uncommitted_count']} uncommitted change(s) in working directory",
            'suggestion': "Run 'se3 commit' to commit changes"
        })

    # Diagnostics: collab session issues
    if collab and collab.get("status") == "active":
        failed = [t for t in collab.get("tasks", []) if t["status"] in ("failed", "escalated")]
        blocked = [t for t in collab.get("tasks", []) if t["status"] == "blocked"]
        if failed:
            issues.append({
                'severity': 'warning',
                'check': 'collab_tasks',
                'message': f"{len(failed)} failed/escalated collab task(s): {', '.join(t['id'] for t in failed)}",
                'suggestion': "Review failed tasks and retry or escalate"
            })
        if blocked:
            issues.append({
                'severity': 'warning',
                'check': 'collab_tasks',
                'message': f"{len(blocked)} blocked collab task(s): {', '.join(t['id'] for t in blocked)}",
                'suggestion': "Check blocked_reason in task files"
            })

    # Diagnostics: human calls
    issues.extend(check_human_calls(root))

    # Count by severity
    errors = [i for i in issues if i['severity'] == 'error']
    warnings = [i for i in issues if i['severity'] == 'warning']
    infos = [i for i in issues if i['severity'] == 'info']

    return {
        'healthy': len(errors) == 0 and len(warnings) == 0,
        'status': status,
        'issues': issues,
        'summary': {
            'errors': len(errors),
            'warnings': len(warnings),
            'info': len(infos)
        }
    }


def print_text_report(results: Dict[str, Any]) -> None:
    """Print a human-readable status report computed from live state."""
    print(f"\n{'=' * 60}")
    print("SE 3.0 Project Status")
    print(f"{'=' * 60}")

    status = results['status']

    # Git info
    print(f"\nBranch: {status.get('branch', 'N/A')}")
    uncommitted = status.get('uncommitted_changes', 0)
    if uncommitted > 0:
        print(f"Uncommitted Changes: {uncommitted}")
    else:
        print(f"Working Tree: clean")

    # Active changes
    changes = status.get('active_changes', [])
    if changes:
        print(f"\nActive Changes:")
        for c in changes:
            print(f"  - {c}")
    else:
        print(f"\nActive Changes: (none)")

    # Collab status
    collab = status.get('collab')
    if collab:
        print(f"\nCollab Session: {collab.get('status', 'unknown')}")
        print(f"  Objective: {collab.get('objective', 'N/A')}")
        tasks = collab.get('tasks', [])
        if tasks:
            for t in tasks:
                print(f"  - {t['id']}: {t['status']} — {t['title']}")

    # Recent activity
    commits = status.get('last_commits', [])
    if commits:
        print(f"\nRecent Commits:")
        for c in commits:
            print(f"  {c}")

    # Diagnostic issues
    print(f"\n{'-' * 60}")
    print("Diagnostics:")
    print(f"{'-' * 60}")

    if results['healthy'] and not results['issues']:
        print("\n  All diagnostics passed")
    else:
        for issue in results['issues']:
            icon = 'x' if issue['severity'] == 'error' else '!' if issue['severity'] == 'warning' else 'i'
            print(f"\n  [{icon}] [{issue['severity'].upper()}] {issue['check']}")
            print(f"      {issue['message']}")
            print(f"      Suggestion: {issue['suggestion']}")

    summary = results['summary']
    print(f"\n{'-' * 60}")
    print(f"Summary: {summary['errors']} errors, {summary['warnings']} warnings, {summary['info']} info")
    print(f"{'=' * 60}\n")


def print_json_report(results: Dict[str, Any]) -> None:
    """Print JSON diagnostic report."""
    print(json.dumps(results, indent=2, default=str))


def main(format: str = "text", project_root: str = ".") -> int:
    """Main entry point for status command."""
    results = run_diagnostics(project_root)

    if format == "json":
        print_json_report(results)
    else:
        print_text_report(results)

    return 0 if results['healthy'] else 1


@app.callback()
def status(
    format: str = typer.Option("text", "--format", "-f", help="Output format (text or json)"),
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
):
    """Check project status and run diagnostics (computed live, no status.md needed)."""
    exit_code = main(format, project_root)
    raise typer.Exit(code=exit_code)
