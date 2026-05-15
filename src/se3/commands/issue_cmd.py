"""SE3 Issue command — Manage project issues.

Provides commands to list, show, create, and reset issues.

Usage:
    se3 issue                        # List open issues
    se3 issue list                   # List open issues
    se3 issue list --all             # List all issues (including closed)
    se3 issue show <id>              # Show issue details
    se3 issue create                 # Create a new issue interactively
    se3 issue reset <id>             # Reset in-progress issue to open
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from ..engine.display import render_block_footer, render_block_header
from ..engine.issue_manager import KNOWN_TYPES, IssueManager, IssueStatus

app = typer.Typer(help="Manage SE3 issues")
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


def _status_color(status: IssueStatus) -> str:
    """Get color for issue status."""
    return {
        IssueStatus.OPEN: "yellow",
        IssueStatus.IN_PROGRESS: "blue",
        IssueStatus.RESOLVED: "green",
        IssueStatus.WONT_FIX: "dim",
        IssueStatus.CLOSED: "green",
    }.get(status, "white")


def _priority_color(priority: str) -> str:
    """Get color for priority."""
    return {
        "critical": "red bold",
        "high": "red",
        "medium": "yellow",
        "low": "dim",
    }.get(priority.lower(), "white")


def _type_color(issue_type: str) -> str:
    """Get color for issue type."""
    return {
        "bug": "red",
        "feature": "green",
        "enhancement": "cyan",
        "idea": "magenta",
        "task": "yellow",
    }.get(issue_type.lower(), "white")


def _format_datetime(dt) -> str:
    """Format datetime to human-readable string."""
    try:
        return dt.strftime("%Y-%m-%d %H:%M")
    except (AttributeError, ValueError):
        return str(dt)


@app.callback(invoke_without_command=True)
def default_cmd(ctx: typer.Context):
    """List open issues (default command)."""
    if ctx.invoked_subcommand is not None:
        return
    list_cmd(show_all=False, type_filter=None)


@app.command(name="list")
def list_cmd(
    show_all: bool = typer.Option(False, "--all", "-a", help="Show all issues including closed"),
    type_filter: Optional[str] = typer.Option(None, "--type", "-t", help="Filter by issue type"),
):
    """List issues."""
    project_root = get_project_root()
    mgr = IssueManager(project_root)
    issues = mgr.list_issues(include_closed=show_all, type_filter=type_filter)

    if not issues:
        label = "open " if not show_all else ""
        typer.echo(f"No {label}issues found.")
        return

    title = "All Issues" if show_all else "Open Issues"
    table = Table(title=title)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Title", style="white")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Priority")
    table.add_column("Tags", style="dim")
    table.add_column("Created", style="dim")

    for issue in issues:
        sc = _status_color(issue.status)
        pc = _priority_color(issue.priority)
        tc = _type_color(issue.type)
        tags_str = ", ".join(issue.tags) if issue.tags else ""
        title_str = issue.title
        if len(title_str) > 50:
            title_str = title_str[:50] + "..."

        table.add_row(
            issue.id,
            title_str,
            f"[{tc}]{issue.type}[/{tc}]",
            f"[{sc}]{issue.status.value}[/{sc}]",
            f"[{pc}]{issue.priority}[/{pc}]",
            tags_str,
            _format_datetime(issue.created_at),
        )

    console.print(table)


@app.command(name="show")
def show_cmd(
    issue_id: str = typer.Argument(..., help="Issue ID to show"),
):
    """Show detailed information about an issue."""
    project_root = get_project_root()
    mgr = IssueManager(project_root)
    issue = mgr.load(issue_id)

    if not issue:
        typer.echo(f"Issue '{issue_id}' not found.", err=True)
        raise typer.Exit(1)

    sc = _status_color(issue.status)
    pc = _priority_color(issue.priority)
    tc = _type_color(issue.type)
    tags_str = ", ".join(issue.tags) if issue.tags else "none"

    content = (
        f"[bold]Title:[/bold] {issue.title}\n"
        f"[bold]Type:[/bold] [{tc}]{issue.type}[/{tc}]\n"
        f"[bold]Status:[/bold] [{sc}]{issue.status.value}[/{sc}]\n"
        f"[bold]Priority:[/bold] [{pc}]{issue.priority}[/{pc}]\n"
        f"[bold]Tags:[/bold] {tags_str}\n"
        f"[bold]Created:[/bold] {_format_datetime(issue.created_at)}\n"
        f"[bold]Updated:[/bold] {_format_datetime(issue.updated_at)}\n"
        f"\n[bold]Description:[/bold]\n{issue.description}"
    )

    render_block_header(f"Issue {issue.id}", "cyan")
    console.print(content)
    console.print()
    render_block_footer("cyan")


def _prompt_field(title: str, message: str, default: str = "") -> Optional[str]:
    """Prompt for a single field with TTY/non-TTY dual-mode support.

    TTY mode: delegates to ``_read_multiline_input`` from ``cli`` — users
    submit with Ctrl+D and cancel with Ctrl+C; Description naturally accepts
    multiple lines.
    Non-TTY mode: reads a single line from ``sys.stdin`` so that piped input
    (e.g. ``CliRunner(input=...)`` in tests) maps one field per line without
    consuming the rest of stdin.
    Empty input falls back to ``default``. Returns ``None`` only when the user
    cancels (Ctrl+C in TTY mode).
    """
    if sys.stdin.isatty():
        # Delayed import to avoid circular import with cli.py
        from ..cli import _read_multiline_input

        result = _read_multiline_input(prompt_title=title, prompt_message=message)
        if result is None:
            return None
        return result if result else default

    line = sys.stdin.readline()
    if not line:
        return default
    line = line.rstrip("\n").strip()
    return line if line else default


@app.command(name="create")
def create_cmd():
    """Create a new issue interactively."""
    project_root = get_project_root()
    mgr = IssueManager(project_root)

    title = _prompt_field("Title", "Enter title (Ctrl+D to submit, Ctrl+C to cancel):")
    if title is None:
        typer.echo("Cancelled.")
        raise typer.Exit(1)

    description = _prompt_field(
        "Description",
        "Enter description (Ctrl+D to submit, Ctrl+C to cancel):",
    )
    if description is None:
        typer.echo("Cancelled.")
        raise typer.Exit(1)

    issue_type = _prompt_field(
        "Type",
        f"Enter type ({'/'.join(KNOWN_TYPES)}) (Ctrl+D to submit, Ctrl+C to cancel):",
        default="bug",
    )
    if issue_type is None:
        typer.echo("Cancelled.")
        raise typer.Exit(1)

    priority = _prompt_field(
        "Priority",
        "Enter priority (low/medium/high/critical) (Ctrl+D to submit, Ctrl+C to cancel):",
        default="medium",
    )
    if priority is None:
        typer.echo("Cancelled.")
        raise typer.Exit(1)

    tags_input = _prompt_field(
        "Tags",
        "Enter tags (comma-separated, or empty) (Ctrl+D to submit, Ctrl+C to cancel):",
        default="",
    )
    if tags_input is None:
        typer.echo("Cancelled.")
        raise typer.Exit(1)

    tags = [t.strip() for t in tags_input.split(",") if t.strip()] if tags_input else []

    issue = mgr.create(
        title=title, description=description, priority=priority, tags=tags, type=issue_type
    )
    typer.echo(f"Created issue {issue.id}: {issue.title}")


@app.command(name="reset")
def reset_cmd(
    issue_id: str = typer.Argument(..., help="Issue ID to reset"),
):
    """Reset an in-progress issue back to open."""
    project_root = get_project_root()
    mgr = IssueManager(project_root)

    try:
        issue = mgr.reset_to_open(issue_id)
        typer.echo(f"Issue {issue.id} reset to open.")
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
