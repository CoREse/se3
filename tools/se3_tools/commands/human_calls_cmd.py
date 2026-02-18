"""Human calls management commands for SE 3.0.

Provides CLI commands for:
- Archiving completed human calls
- Listing pending and archived calls
"""

import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import typer

from ..human_calls import HumanCallStore, CallStatus

app = typer.Typer(help="Manage human calls")


def find_project_root() -> Path:
    """Find the project root by looking for .claude/ directory."""
    current = Path.cwd()
    while current != current.parent:
        if (current / ".claude").is_dir():
            return current
        current = current.parent
    return Path.cwd()


def get_calls_dir(project_root: Path) -> Path:
    """Get the human-calls directory."""
    return project_root / "human-calls"


def get_archive_dir(project_root: Path) -> Path:
    """Get the human-calls/archive directory."""
    return project_root / "human-calls" / "archive"


def ensure_archive_dir(project_root: Path) -> Path:
    """Ensure the archive directory exists."""
    archive_dir = get_archive_dir(project_root)
    archive_dir.mkdir(parents=True, exist_ok=True)
    return archive_dir


def is_call_completed(call) -> bool:
    """Check if a call has a meaningful response (completed).

    A call is considered completed if:
    - Status is RESPONDED or COMPLETED
    - The response section has meaningful content
    """
    if call.status in (CallStatus.RESPONDED, CallStatus.COMPLETED):
        return True

    # Also check if there's a meaningful response even if status doesn't reflect it
    if call.response and len(call.response.strip()) >= 10:
        # Check it's not just the default prompt marker
        default_markers = [
            "<!-- Human: write your response below -->",
            "<!-- 人类：请在下方输入您的回复 -->",
            "<!-- Humain: écrivez votre réponse ci-dessous -->",
        ]
        for marker in default_markers:
            if marker in call.response:
                return False
        return True

    return False


@app.command(name="archive")
def archive_calls(
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Preview what would be archived without moving files"),
    days: int = typer.Option(30, "--days", "-d", help="Archive calls completed more than N days ago"),
    all_completed: bool = typer.Option(False, "--all-completed", "-a", help="Archive all completed calls regardless of age"),
    project_root: str = typer.Option(None, "--project-root", "-p", help="Project root directory"),
):
    """Archive completed/expired human calls to human-calls/archive/.

    By default, archives calls completed more than 30 days ago.
    Archived calls are moved to human-calls/archive/.

    Examples:
        se3 human-calls archive                    # Archive calls > 30 days old
        se3 human-calls archive --days 7           # Archive calls > 7 days old
        se3 human-calls archive --dry-run          # Preview what would be archived
        se3 human-calls archive --all-completed    # Archive all completed calls
    """
    root = Path(project_root) if project_root else find_project_root()
    calls_dir = get_calls_dir(root)

    if not calls_dir.exists():
        typer.echo("No human-calls directory found.")
        raise typer.Exit(0)

    store = HumanCallStore(calls_dir)

    # Use the store's archive method
    archived = store.archive_completed_calls(
        days_old=days,
        dry_run=dry_run,
        all_completed=all_completed
    )

    if not archived:
        typer.echo("No completed calls to archive.")
        raise typer.Exit(0)

    if dry_run:
        typer.echo(f"Would archive {len(archived)} call(s):")
        for info in archived:
            typer.echo(f"  - {Path(info['file_path']).name}")
        raise typer.Exit(0)

    # Display results
    typer.echo(f"Archived {len(archived)} call(s) to human-calls/archive/:")
    for info in archived:
        typer.echo(f"  - {Path(info['file_path']).name} -> archive/{Path(info['archive_path']).name}")


@app.command(name="list")
def list_calls(
    status: str = typer.Option("all", "--status", "-s", help="Filter by status: pending, responded, completed, archived, all"),
    project_root: str = typer.Option(None, "--project-root", "-p", help="Project root directory"),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum number of calls to show"),
):
    """List human calls - pending, responded, or archived."""
    root = Path(project_root) if project_root else find_project_root()
    calls_dir = get_calls_dir(root)
    archive_dir = get_archive_dir(root)

    if status == "archived":
        # List archived calls only
        if not archive_dir.exists():
            typer.echo("No archive directory found.")
            raise typer.Exit(0)

        archived_files = sorted(archive_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)

        if not archived_files:
            typer.echo("No archived calls.")
            raise typer.Exit(0)

        typer.echo(f"\n{'=' * 60}")
        typer.echo("Archived Human Calls")
        typer.echo(f"{'=' * 60}")

        for filepath in archived_files[:limit]:
            stat = filepath.stat()
            archived_date = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            typer.echo(f"  {filepath.name}")
            typer.echo(f"    Archived: {archived_date}")

        if len(archived_files) > limit:
            typer.echo(f"\n  ... and {len(archived_files) - limit} more")

        typer.echo(f"{'=' * 60}")
        typer.echo(f"Total archived: {len(archived_files)}")

    elif not calls_dir.exists():
        typer.echo("No human-calls directory found.")
        raise typer.Exit(0)

    else:
        store = HumanCallStore(calls_dir)

        if status == "pending":
            calls = store.get_pending_calls()
            title = "Pending Human Calls (awaiting response)"
        elif status == "responded":
            calls = store.get_responded_calls()
            title = "Responded Human Calls (waiting to be processed)"
        elif status == "completed":
            all_calls = store.get_all_calls(include_completed=True)
            calls = [c for c in all_calls if c.status == CallStatus.COMPLETED]
            title = "Completed Human Calls"
        else:  # "all"
            calls = store.get_all_calls(include_completed=True)
            title = "All Human Calls"

        if not calls:
            typer.echo(f"No {status} calls found.")
            raise typer.Exit(0)

        typer.echo(f"\n{'=' * 60}")
        typer.echo(title)
        typer.echo(f"{'=' * 60}")

        for call in calls[:limit]:
            status_icon = {
                CallStatus.PENDING: "○",
                CallStatus.RESPONDED: "◉",
                CallStatus.PROCESSING: "◎",
                CallStatus.COMPLETED: "●",
                CallStatus.EXPIRED: "⊘",
            }.get(call.status, "?")

            created_str = call.created.strftime("%Y-%m-%d %H:%M") if call.created else "unknown"

            typer.echo(f"  {status_icon} {call.file_path.name}")
            typer.echo(f"    Title: {call.title or '(no title)'}")
            typer.echo(f"    Status: {call.status.value}")
            typer.echo(f"    Created: {created_str}")
            if call.response_timestamp:
                typer.echo(f"    Responded: {call.response_timestamp.strftime('%Y-%m-%d %H:%M')}")
            typer.echo()

        if len(calls) > limit:
            typer.echo(f"  ... and {len(calls) - limit} more")

        typer.echo(f"{'=' * 60}")
        typer.echo(f"Total: {len(calls)}")

    typer.echo()


def get_pending_calls_count(project_root: Path) -> int:
    """Get the count of pending human calls.

    Used by collab --status to show pending calls.
    """
    calls_dir = get_calls_dir(project_root)
    if not calls_dir.exists():
        return 0

    store = HumanCallStore(calls_dir)
    return len(store.get_pending_calls())


def get_responded_calls_count(project_root: Path) -> int:
    """Get the count of responded human calls.

    Used by collab --status to show calls waiting to be processed.
    """
    calls_dir = get_calls_dir(project_root)
    if not calls_dir.exists():
        return 0

    store = HumanCallStore(calls_dir)
    return len(store.get_responded_calls())
