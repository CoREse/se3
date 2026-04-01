"""SE3 History command — View and manage session history.

Provides commands to list flows, show flow details, restore previous sessions,
and list archived flows.

Usage:
    se3 history                          # List all flows
    se3 history list                     # List all flows
    se3 history show <flow_id>           # Show flow details
    se3 history restore <flow_id>        # Restore a flow
    se3 history archived                 # List archived flows
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer
from rich.console import Console
from rich.table import Table

# Import necessary modules from the engine
from ..engine.persistence import PersistenceManager
from ..engine.chat_history import list_flows as list_chat_flows, get_flow_history

app = typer.Typer(help="View and manage session history")
console = Console()


def get_project_root() -> Path:
    """Find project root by looking for .git directory or se3.yaml."""
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".git").exists():
            return parent
        if (parent / "se3.yaml").exists() or (parent / "se3.config.yaml").exists():
            return parent
    return cwd


def format_datetime(iso_string: str) -> str:
    """Format ISO datetime string to human-readable format."""
    try:
        dt = datetime.fromisoformat(iso_string)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_string


def format_duration(created: str, updated: str) -> str:
    """Calculate and format duration between two timestamps."""
    try:
        dt_created = datetime.fromisoformat(created)
        dt_updated = datetime.fromisoformat(updated)
        duration = dt_updated - dt_created

        total_minutes = int(duration.total_seconds() / 60)
        if total_minutes < 60:
            return f"{total_minutes}m"
        hours = total_minutes // 60
        minutes = total_minutes % 60
        if hours < 24:
            return f"{hours}h {minutes}m"
        days = hours // 24
        hours = hours % 24
        return f"{days}d {hours}h"
    except (ValueError, TypeError):
        return "unknown"


def list_active_flows(project_root: Path) -> List[Dict[str, Any]]:
    """List all active flows from persistence manager."""
    persistence = PersistenceManager(project_root)
    return persistence.list_active_flows()


def list_all_flows(project_root: Path) -> List[Dict[str, Any]]:
    """List all flows from all data sources."""
    persistence = PersistenceManager(project_root)
    return persistence.list_all_flows()


def list_archived_flows_from_disk(project_root: Path) -> List[Dict[str, Any]]:
    """List all archived flows from the archive directory."""
    archive_dir = project_root / "se3" / "state" / "archive"
    if not archive_dir.exists():
        return []

    archived = []
    for archive_file in sorted(archive_dir.glob("engine_*.json")):
        try:
            # Extract timestamp from filename
            timestamp_str = archive_file.stem.replace("engine_", "")
            dt = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")

            # Load flow data to get more details
            content = archive_file.read_text(encoding="utf-8")
            data = json.loads(content)

            archived.append({
                "flow_id": data.get("flow_id", "unknown"),
                "status": data.get("status", "unknown"),
                "task_description": data.get("task_description", "No description")[:100],
                "archived_at": dt.isoformat(),
                "file": archive_file.name,
            })
        except (ValueError, json.JSONDecodeError, IOError):
            # Skip malformed archive files
            continue

    return archived


def get_flow_detail(project_root: Path, flow_id: str) -> Optional[Dict[str, Any]]:
    """Get detailed information about a specific flow."""
    persistence = PersistenceManager(project_root)

    # Load the flow
    flow = persistence.load_flow()
    if not flow or flow.flow_id != flow_id:
        return None

    # Get chat history for this flow
    chat_sessions = get_flow_history(project_root, flow_id)

    # Build step details
    step_details = []
    for step_id in flow.state.step_history:
        step = flow.state.steps.get(step_id)
        if not step:
            continue

        step_info = {
            "step_id": step.step_id,
            "step_type": step.step_type.value,
            "status": step.status.value,
            "started_at": step.started_at.isoformat() if step.started_at else None,
            "completed_at": step.completed_at.isoformat() if step.completed_at else None,
            "retry_count": step.retry_count,
            "error_message": step.error_message,
        }
        step_details.append(step_info)

    # Calculate progress
    completed, total = flow.get_progress()

    return {
        "flow_id": flow.flow_id,
        "status": flow.status.value,
        "task_description": flow.task_description,
        "task_type": flow.task_type,
        "change_name": flow.change_name,
        "created_at": flow.created_at.isoformat(),
        "updated_at": flow.updated_at.isoformat(),
        "completed_at": flow.completed_at.isoformat() if flow.completed_at else None,
        "is_loop_mode": flow.is_loop_mode,
        "progress": {"completed": completed, "total": total},
        "current_step_id": flow.state.current_step_id,
        "steps": step_details,
        "chat_sessions": len(chat_sessions),
    }


def _status_color(status: str) -> str:
    """Get color for status."""
    return {
        "completed": "green",
        "failed": "red",
        "running": "yellow",
        "init": "blue",
        "paused": "magenta",
    }.get(status.lower(), "white")


def _step_status_color(status: str) -> str:
    """Get color for step status."""
    return {
        "completed": "green",
        "failed": "red",
        "running": "yellow",
        "pending": "dim",
        "retrying": "magenta",
        "paused": "cyan",
    }.get(status.lower(), "white")


def _render_flows_table(flows: List[Dict[str, Any]], title: str) -> None:
    """Render a list of flows as a Rich table."""
    table = Table(title=title)
    table.add_column("Flow ID", style="cyan", no_wrap=True)
    table.add_column("Status")
    table.add_column("Task Description", style="white")
    table.add_column("Progress", justify="right")
    table.add_column("Updated", style="dim")
    table.add_column("Source", style="dim")

    status_colors = {
        "completed": "green",
        "failed": "red",
        "running": "yellow",
        "init": "blue",
        "paused": "magenta",
        "history": "dim",
    }

    for flow in flows:
        flow_id = flow.get("flow_id", "unknown")
        status = flow.get("status", "unknown")
        desc = flow.get("task_description", "No description")
        if len(desc) > 50:
            desc = desc[:50] + "..."
        progress = flow.get("progress", "-")
        updated = format_datetime(flow.get("updated_at", ""))
        source = flow.get("source", "")

        color = status_colors.get(status.lower(), "white")
        table.add_row(
            flow_id,
            f"[{color}]{status}[/{color}]",
            desc,
            progress,
            updated,
            source,
        )

    console.print(table)
    typer.echo("\nUse 'se3 history show <flow_id>' to view details of a specific flow.")


# Default command - list flows
@app.callback(invoke_without_command=True)
def default_cmd(
    ctx: typer.Context,
    active_only: bool = typer.Option(False, "--active-only", help="Show only the active flow"),
    archived_only: bool = typer.Option(False, "--archived-only", "-a", help="Show only archived flows"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List all flows (active, archived, and history)."""
    # If a subcommand is being invoked, skip this
    if ctx.invoked_subcommand is not None:
        return

    project_root = get_project_root()
    flows = list_all_flows(project_root)

    if active_only:
        flows = [f for f in flows if f.get("source") == "active"]
        title = "Active Flows"
    elif archived_only:
        flows = [f for f in flows if f.get("source") == "archived"]
        title = "Archived Flows"
    else:
        title = "All Flows"

    if json_output:
        typer.echo(json.dumps(flows, indent=2, default=str))
        return

    if not flows:
        typer.echo(f"No {title.lower()} found.")
        return

    _render_flows_table(flows, title)


@app.command(name="list")
def list_cmd(
    active_only: bool = typer.Option(False, "--active-only", help="Show only the active flow"),
    archived_only: bool = typer.Option(False, "--archived-only", "-a", help="Show only archived flows"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List all flows (active, archived, and history)."""
    project_root = get_project_root()
    flows = list_all_flows(project_root)

    if active_only:
        flows = [f for f in flows if f.get("source") == "active"]
        title = "Active Flows"
    elif archived_only:
        flows = [f for f in flows if f.get("source") == "archived"]
        title = "Archived Flows"
    else:
        title = "All Flows"

    if json_output:
        typer.echo(json.dumps(flows, indent=2, default=str))
        return

    if not flows:
        typer.echo(f"No {title.lower()} found.")
        return

    _render_flows_table(flows, title)


@app.command(name="show")
def show_cmd(
    flow_id: str = typer.Argument(..., help="Flow ID to show details for"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Show detailed information about a specific flow."""
    project_root = get_project_root()

    detail = get_flow_detail(project_root, flow_id)

    if not detail:
        # Try to find partial matches
        all_flows = list_active_flows(project_root)
        matches = [f for f in all_flows if f.get("flow_id", "").startswith(flow_id)]

        if len(matches) == 1:
            detail = get_flow_detail(project_root, matches[0]["flow_id"])
        elif len(matches) > 1:
            typer.echo(f"Multiple flows match '{flow_id}':")
            for m in matches:
                typer.echo(f"  - {m.get('flow_id', 'unknown')}")
            raise typer.Exit(1)

    if not detail:
        typer.echo(f"Flow '{flow_id}' not found.", err=True)
        raise typer.Exit(1)

    if json_output:
        typer.echo(json.dumps(detail, indent=2, default=str))
        return

    # Display formatted details
    console.print(f"\n[bold cyan]Flow Details: {detail['flow_id']}[/bold cyan]\n")

    # Basic info table
    info_table = Table(show_header=False, box=None)
    info_table.add_column("Key", style="bold")
    info_table.add_column("Value")

    info_table.add_row("Status", f"[{_status_color(detail['status'])}]{detail['status']}[/{_status_color(detail['status'])}]")
    info_table.add_row("Task", detail['task_description'])
    if detail.get('task_type'):
        info_table.add_row("Type", detail['task_type'])
    if detail.get('change_name'):
        info_table.add_row("Change", detail['change_name'])
    info_table.add_row("Progress", f"{detail['progress']['completed']}/{detail['progress']['total']}")
    info_table.add_row("Created", format_datetime(detail['created_at']))
    info_table.add_row("Updated", format_datetime(detail['updated_at']))
    if detail.get('completed_at'):
        info_table.add_row("Completed", format_datetime(detail['completed_at']))
    info_table.add_row("Chat Sessions", str(detail['chat_sessions']))

    console.print(info_table)

    # Steps table
    if detail['steps']:
        console.print(f"\n[bold]Steps:[/bold]")
        steps_table = Table()
        steps_table.add_column("#", justify="right")
        steps_table.add_column("Step Type")
        steps_table.add_column("Status")
        steps_table.add_column("Retries", justify="right")
        steps_table.add_column("Error", style="red")

        for i, step in enumerate(detail['steps'], 1):
            status_color = _step_status_color(step['status'])
            error_msg = step.get('error_message', '') or ""
            if error_msg and len(error_msg) > 40:
                error_msg = error_msg[:40] + "..."

            steps_table.add_row(
                str(i),
                step['step_type'],
                f"[{status_color}]{step['status']}[/{status_color}]",
                str(step.get('retry_count', 0)),
                error_msg,
            )
        console.print(steps_table)

    console.print(f"\n[dim]Use 'se3 history restore {detail['flow_id']}' to resume this flow.[/dim]\n")


@app.command(name="restore")
def restore_cmd(
    flow_id: str = typer.Argument(..., help="Flow ID to restore"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Show what would be done without executing"),
):
    """Restore a previous session by resuming the flow.

    This delegates to 'se3 run --resume --flow-id <flow_id>'.
    """
    project_root = get_project_root()

    # Validate flow exists
    persistence = PersistenceManager(project_root)
    flow = persistence.load_flow()

    if not flow or flow.flow_id != flow_id:
        # Try to find by prefix
        all_flows = list_active_flows(project_root)
        matches = [f for f in all_flows if f.get("flow_id", "").startswith(flow_id)]

        if len(matches) == 1:
            flow_id = matches[0]["flow_id"]
        elif len(matches) > 1:
            typer.echo(f"Multiple flows match '{flow_id}':")
            for m in matches:
                typer.echo(f"  - {m.get('flow_id', 'unknown')}")
            raise typer.Exit(1)
        else:
            typer.echo(f"Flow '{flow_id}' not found.", err=True)
            raise typer.Exit(1)

    if dry_run:
        typer.echo(f"Would restore flow: {flow_id}")
        typer.echo(f"Command: se3 run --resume --flow-id {flow_id}")
        return

    # Delegate to se3 run --resume
    typer.echo(f"Restoring flow: {flow_id}")
    result = subprocess.run(
        ["se3", "run", "--resume", "--flow-id", flow_id],
        cwd=project_root,
    )
    raise typer.Exit(result.returncode)


@app.command(name="archived")
def archived_cmd(
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List all archived flows."""
    project_root = get_project_root()

    archived = list_archived_flows_from_disk(project_root)

    if json_output:
        typer.echo(json.dumps(archived, indent=2, default=str))
        return

    if not archived:
        typer.echo("No archived flows found.")
        return

    table = Table(title="Archived Flows")
    table.add_column("Flow ID", style="cyan", no_wrap=True)
    table.add_column("Status", style="green")
    table.add_column("Task Description", style="white")
    table.add_column("Archived At", style="dim")

    for flow in archived:
        flow_id = flow["flow_id"]
        status = flow["status"]
        desc = flow["task_description"]
        if len(desc) > 50:
            desc = desc[:50] + "..."
        archived_at = format_datetime(flow["archived_at"])

        status_style = {
            "completed": "green",
            "failed": "red",
            "running": "yellow",
        }.get(status.lower(), "white")

        table.add_row(
            flow_id,
            f"[{status_style}]{status}[/{status_style}]",
            desc,
            archived_at,
        )

    console.print(table)


def _status_color(status: str) -> str:
    """Get color for status."""
    return {
        "completed": "green",
        "failed": "red",
        "running": "yellow",
        "init": "blue",
        "paused": "magenta",
    }.get(status.lower(), "white")


def _step_status_color(status: str) -> str:
    """Get color for step status."""
    return {
        "completed": "green",
        "failed": "red",
        "running": "yellow",
        "pending": "dim",
        "retrying": "magenta",
        "paused": "cyan",
    }.get(status.lower(), "white")


if __name__ == "__main__":
    app()
