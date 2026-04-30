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
from ..engine.chat_history import (
    list_flows as list_chat_flows,
    get_flow_history,
    get_detailed_json,
    interleave_sessions_for_display,
    render_session_detailed,
)

app = typer.Typer(help="View and manage session history")
console = Console()


def get_project_root() -> Path:
    """Find project root by looking for .git directory or an SE3 config file."""
    from ..config import is_se3_project_root

    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".git").exists():
            return parent
        if is_se3_project_root(parent):
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


def _detail_from_flow(project_root: Path, flow: Any) -> Dict[str, Any]:
    """Build detail dict from a FlowInstance object."""
    chat_sessions = get_flow_history(project_root, flow.flow_id)

    step_details = []
    for step_id in flow.state.step_history:
        step = flow.state.steps.get(step_id)
        if not step:
            continue
        step_details.append({
            "step_id": step.step_id,
            "step_type": step.step_type.value,
            "status": step.status.value,
            "started_at": step.started_at.isoformat() if step.started_at else None,
            "completed_at": step.completed_at.isoformat() if step.completed_at else None,
            "retry_count": step.retry_count,
            "error_message": step.error_message,
            "outputs": step.outputs,
        })

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


def _load_archived_flow(project_root: Path, flow_id: str) -> Optional[Any]:
    """Try to load a FlowInstance from archive files matching flow_id."""
    from ..engine.models import FlowInstance

    archive_dir = project_root / "se3" / "state" / "archive"
    if not archive_dir.exists():
        return None

    for archive_file in archive_dir.glob("engine_*.json"):
        try:
            data = json.loads(archive_file.read_text(encoding="utf-8"))
            if data.get("flow_id") == flow_id:
                return FlowInstance.from_dict(data)
        except (json.JSONDecodeError, KeyError, ValueError, IOError):
            continue
    return None


def _detail_from_history(project_root: Path, flow_id: str) -> Optional[Dict[str, Any]]:
    """Build a minimal detail dict from history-only data."""
    history_dir = project_root / "se3" / "history" / flow_id
    if not history_dir.is_dir():
        return None

    chat_sessions = get_flow_history(project_root, flow_id)

    # Try to read _meta.json for timestamps
    meta_path = history_dir / "_meta.json"
    created_at = ""
    task_type = None
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            created_at = meta.get("created_at", "")
            task_type = meta.get("type")
        except (json.JSONDecodeError, IOError):
            pass

    # Derive updated_at from latest file mtime
    try:
        latest_mtime = max(
            (f.stat().st_mtime for f in history_dir.iterdir() if f.is_file()),
            default=0,
        )
        updated_at = (
            datetime.fromtimestamp(latest_mtime).isoformat() if latest_mtime else ""
        )
    except Exception:
        updated_at = ""

    # Extract task description from chat history
    task_description = PersistenceManager.extract_history_summary(history_dir)

    # Build step list from chat session step IDs.
    # For history-only flows step.outputs are not preserved, but we can
    # reconstruct self_check pass numbering by counting consecutive self_check
    # sessions (resetting the counter at any non-self_check step).
    from ..config import WorkflowConfig

    try:
        passes_required = WorkflowConfig.load(project_root).self_check_passes_required
    except Exception:
        passes_required = None

    step_details = []
    sc_run_index = 0
    for session in chat_sessions:
        outputs = {}
        if session.step_type == "self_check":
            sc_run_index += 1
            outputs["self_check_pass_index"] = sc_run_index
            outputs["self_check_passes_required"] = (
                passes_required if passes_required is not None else sc_run_index
            )
        else:
            sc_run_index = 0
        step_details.append({
            "step_id": session.step_id,
            "step_type": session.step_type,
            "status": "completed",
            "started_at": None,
            "completed_at": None,
            "retry_count": 0,
            "error_message": None,
            "outputs": outputs,
        })

    return {
        "flow_id": flow_id,
        "status": "history",
        "task_description": task_description,
        "task_type": task_type,
        "change_name": None,
        "created_at": created_at,
        "updated_at": updated_at,
        "completed_at": None,
        "is_loop_mode": False,
        "progress": {"completed": len(step_details), "total": len(step_details)},
        "current_step_id": None,
        "steps": step_details,
        "chat_sessions": len(chat_sessions),
    }


def get_flow_detail(project_root: Path, flow_id: str) -> Optional[Dict[str, Any]]:
    """Get detailed information about a specific flow.

    Searches across three data sources in order:
    1. Active flow (se3/state/engine.json)
    2. Archived flows (se3/state/archive/engine_*.json)
    3. History-only flows (se3/history/{flow_id}/)
    """
    persistence = PersistenceManager(project_root)

    # 1. Active flow
    flow = persistence.load_flow()
    if flow and flow.flow_id == flow_id:
        return _detail_from_flow(project_root, flow)

    # 2. Archived flow
    archived_flow = _load_archived_flow(project_root, flow_id)
    if archived_flow:
        return _detail_from_flow(project_root, archived_flow)

    # 3. History-only flow
    return _detail_from_history(project_root, flow_id)


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
    detailed: bool = typer.Option(False, "--detailed", "-d", help="Show LLM call details for each step"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show full response including tool calls (requires --detailed)"),
):
    """Show detailed information about a specific flow."""
    # --verbose implies --detailed
    if verbose and not detailed:
        detailed = True

    project_root = get_project_root()

    detail = get_flow_detail(project_root, flow_id)

    if not detail:
        # Try to find partial matches across all sources
        all_flows = list_all_flows(project_root)
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

    if json_output and not detailed:
        typer.echo(json.dumps(detail, indent=2, default=str))
        return

    if json_output and detailed:
        _show_detailed_json(project_root, detail)
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

            # Surface self_check pass numbering (#i/N) when available
            step_label = step['step_type']
            outputs = step.get('outputs', {})
            if step['step_type'] == 'self_check':
                pass_index = outputs.get('self_check_pass_index')
                passes_required = outputs.get('self_check_passes_required')
                if pass_index is not None and passes_required is not None:
                    step_label = f"self_check #{pass_index}/{passes_required}"

            steps_table.add_row(
                str(i),
                step_label,
                f"[{status_color}]{step['status']}[/{status_color}]",
                str(step.get('retry_count', 0)),
                error_msg,
            )
        console.print(steps_table)

    # Detailed LLM call display
    if detailed:
        _show_detailed_sessions(project_root, detail['flow_id'], verbose=verbose)

    console.print(f"\n[dim]Use 'se3 history restore {detail['flow_id']}' to resume this flow.[/dim]\n")


def _show_detailed_sessions(
    project_root: Path, flow_id: str, verbose: bool = False
) -> None:
    """Render detailed LLM call sessions for a flow."""
    from rich.rule import Rule

    sessions = get_flow_history(project_root, flow_id)
    if not sessions:
        console.print("\n[dim]No chat history available for this flow.[/dim]")
        return

    sessions = interleave_sessions_for_display(sessions)

    console.print(f"\n[bold]LLM Call Details:[/bold]")

    for session in sessions:
        console.print(Rule(
            f"{session.step_type} (id: {session.step_id})",
            style="cyan",
        ))
        renderables = render_session_detailed(session, verbose=verbose)
        for r in renderables:
            console.print(r)


def _show_detailed_json(project_root: Path, detail: dict) -> None:
    """Output detailed flow info with chat history as JSON."""
    flow_id = detail["flow_id"]
    output = {**detail, "chat_history": get_detailed_json(project_root, flow_id)}
    typer.echo(json.dumps(output, indent=2, default=str))


@app.command(name="restore")
def restore_cmd(
    flow_id: str = typer.Argument(..., help="Flow ID to restore"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Show what would be done without executing"),
):
    """Restore a previous session by resuming the flow.

    This delegates to 'se3 run --resume --flow-id <flow_id>'.
    """
    project_root = get_project_root()

    # Validate flow exists across all sources (active, archived, history)
    all_flows = list_all_flows(project_root)
    exact = [f for f in all_flows if f.get("flow_id") == flow_id]

    if not exact:
        # Try to find by prefix
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


if __name__ == "__main__":
    app()
