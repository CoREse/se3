"""SE3 Dashboard command - Comprehensive project status overview.

Provides a unified view of:
- Flow engine status
- Active changes
- Git status
- Recent activity
- Health indicators
"""

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer

from ..utils import discover_changes
from ..human_calls import HumanCallStore

app = typer.Typer(invoke_without_command=True)


def get_terminal_width() -> int:
    """Get terminal width, default to 80 if not available."""
    try:
        import os
        return os.get_terminal_size().columns
    except OSError:
        return 80


def print_header(title: str, width: int = 60):
    """Print a formatted header."""
    print(f"\n{'=' * width}")
    print(f" {title}")
    print(f"{'=' * width}")


def print_section(title: str, width: int = 60):
    """Print a section header."""
    print(f"\n{'─' * width}")
    print(f" {title}")
    print(f"{'─' * width}")


def compute_git_status(project_root: Path) -> Dict[str, Any]:
    """Compute current git state."""
    info = {
        "branch": "unknown",
        "uncommitted_count": 0,
        "uncommitted_files": [],
        "last_commit": None,
        "commit_count_ahead": 0,
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
        info["uncommitted_files"] = [line[3:] for line in lines if len(line) > 3]

    # Last commit
    result = subprocess.run(
        ["git", "log", "-1", "--format=%h %s (%ar)"],
        cwd=project_root, capture_output=True, text=True
    )
    if result.returncode == 0:
        info["last_commit"] = result.stdout.strip()

    # Check if ahead of remote
    result = subprocess.run(
        ["git", "rev-list", "--count", "HEAD...@{u}"],
        cwd=project_root, capture_output=True, text=True
    )
    if result.returncode == 0:
        try:
            info["commit_count_ahead"] = int(result.stdout.strip())
        except ValueError:
            pass

    return info


def compute_flow_engine_status(project_root: Path) -> Optional[Dict[str, Any]]:
    """Compute flow engine status from .se3/state/ directory."""
    state_dir = project_root / ".se3" / "state"

    if not state_dir.exists():
        return None

    flows = []
    engine_file = state_dir / "engine.json"
    if engine_file.exists():
        try:
            with open(engine_file) as f:
                data = json.load(f)
                state_data = data.get("state", {})
                flows.append({
                    "id": data.get("flow_id", "unknown"),
                    "status": data.get("status", "unknown"),
                    "description": data.get("task_description", "No description"),
                    "current_step": state_data.get("current_step_id", "none"),
                    "step_status": data.get("current_step_status", "unknown"),
                    "created_at": data.get("created_at", "unknown"),
                    "updated_at": data.get("updated_at", "unknown"),
                })
        except (json.JSONDecodeError, IOError):
            pass

    active = [f for f in flows if f["status"] == "in_progress"]
    completed = [f for f in flows if f["status"] == "completed"]
    failed = [f for f in flows if f["status"] == "failed"]

    return {
        "initialized": True,
        "total": len(flows),
        "active": active,
        "completed": completed,
        "failed": failed,
    }


def compute_changes_status(project_root: Path) -> Dict[str, Any]:
    """Compute changes status (legacy openspec/changes/)."""
    changes_dir = project_root / "openspec" / "changes"

    if not changes_dir.exists():
        return {"total": 0, "active": [], "archived": []}

    active = []
    archived = []

    for state_file in changes_dir.rglob(".openspec.yaml"):
        change_path = state_file.parent.relative_to(changes_dir)
        change_name = str(change_path)

        if change_name.startswith("archive/"):
            archived.append(change_name)
        else:
            # Check for tasks.md to count progress
            tasks_file = state_file.parent / "tasks.md"
            total_tasks = 0
            completed_tasks = 0
            if tasks_file.exists():
                try:
                    content = tasks_file.read_text()
                    total_tasks = content.count("- [")
                    completed_tasks = content.count("- [x]")
                except IOError:
                    pass

            active.append({
                "name": change_name,
                "progress": f"{completed_tasks}/{total_tasks}",
                "percent": (completed_tasks / total_tasks * 100) if total_tasks > 0 else 0,
            })

    return {
        "total": len(active) + len(archived),
        "active": active,
        "archived_count": len(archived),
    }


def compute_recent_activity(project_root: Path, days: int = 7) -> List[Dict[str, Any]]:
    """Compute recent activity from git log."""
    result = subprocess.run(
        ["git", "log", f"--since={days} days ago", "--format=%h|%s|%ar|%an"],
        cwd=project_root, capture_output=True, text=True
    )

    activity = []
    if result.returncode == 0 and result.stdout.strip():
        for line in result.stdout.strip().split("\n")[:10]:
            parts = line.split("|", 3)
            if len(parts) >= 3:
                activity.append({
                    "hash": parts[0],
                    "message": parts[1],
                    "when": parts[2],
                    "author": parts[3] if len(parts) > 3 else "Unknown",
                })

    return activity


def print_dashboard(project_root: Path):
    """Print the dashboard."""
    width = min(get_terminal_width(), 80)

    # Header
    print_header("SE3 Project Dashboard", width)
    print(f" Project: {project_root.name}")
    print(f" Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # Git Status Section
    print_section("Git Status", width)
    git = compute_git_status(project_root)
    print(f" Branch: {git['branch']}")
    if git['uncommitted_count'] > 0:
        print(f" Uncommitted: {git['uncommitted_count']} file(s)")
        for f in git['uncommitted_files'][:5]:
            print(f"   - {f}")
        if len(git['uncommitted_files']) > 5:
            print(f"   ... and {len(git['uncommitted_files']) - 5} more")
    else:
        print(f" Working Tree: clean")

    if git['last_commit']:
        print(f" Last Commit: {git['last_commit']}")

    if git['commit_count_ahead'] > 0:
        print(f" Ahead of Remote: {git['commit_count_ahead']} commit(s)")

    # Flow Engine Section
    print_section("Flow Engine", width)
    flow = compute_flow_engine_status(project_root)
    if flow:
        print(f" Status: Active ({flow['total']} total flow(s))")

        if flow['active']:
            print(f"\n In Progress:")
            for f in flow['active']:
                print(f"   ▶ {f['id'][:20]:20} - {f['current_step']}")
                desc = f['description'][:40] + "..." if len(f['description']) > 40 else f['description']
                print(f"     {desc}")

        if flow['failed']:
            print(f"\n Failed:")
            for f in flow['failed']:
                print(f"   ✗ {f['id'][:20]:20}")

        if flow['completed']:
            print(f"\n Completed: {len(flow['completed'])} flow(s)")
    else:
        print(f" Status: Not Initialized")
        print(f" Tip: Use 'se3 run' to start using the flow engine")

    # Changes Section
    print_section("OpenSpec Changes", width)
    changes = compute_changes_status(project_root)
    print(f" Total: {changes['total']} ({len(changes['active'])} active, {changes['archived_count']} archived)")

    if changes['active']:
        print(f"\n Active Changes:")
        for c in changes['active']:
            bar_width = 20
            filled = int(c['percent'] / 100 * bar_width)
            bar = "█" * filled + "░" * (bar_width - filled)
            print(f"   {bar} {c['progress']:>7} {c['name'][:30]}")

    # Recent Activity
    print_section("Recent Activity (7 days)", width)
    activity = compute_recent_activity(project_root)
    if activity:
        for a in activity[:5]:
            msg = a['message'][:40] + "..." if len(a['message']) > 40 else a['message']
            print(f"   {a['hash']} {msg} ({a['when']})")
    else:
        print("   No recent commits")

    # Quick Actions
    print_section("Quick Actions", width)
    print("   se3 run 'task description'  - Start a new flow")
    print("   se3 run --resume            - Resume interrupted flow")
    print("   se3 status                  - Detailed diagnostics")
    print("   se3 collab list             - View collaboration tasks")

    # Footer
    print(f"\n{'=' * width}\n")


@app.callback()
def dashboard(
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Display a comprehensive project status dashboard.

    Shows flow engine status, active changes, git state, and recent activity
    in a unified overview.

    Examples:
        se3 dashboard
        se3 dashboard --json
    """
    root = Path(project_root).resolve()

    if json_output:
        # Output JSON
        data = {
            "project": root.name,
            "timestamp": datetime.now().isoformat(),
            "git": compute_git_status(root),
            "flow_engine": compute_flow_engine_status(root),
            "changes": compute_changes_status(root),
            "recent_activity": compute_recent_activity(root),
        }
        print(json.dumps(data, indent=2, default=str))
    else:
        print_dashboard(root)


if __name__ == "__main__":
    app()
