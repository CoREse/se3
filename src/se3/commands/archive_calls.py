"""Archive completed human calls command for SE3."""

from pathlib import Path
from typing import Optional

import typer

from ..human_calls import HumanCallStore

app = typer.Typer(name="archive-calls", help="Archive completed human calls")


def _get_calls_dir(project_root: Path) -> Path:
    """Get the human-calls directory path."""
    # Try common locations
    calls_dir = project_root / "human-calls"
    if calls_dir.exists():
        return calls_dir

    # Check if there's a .claude directory structure
    claude_calls = project_root / ".claude" / "human-calls"
    if claude_calls.exists():
        return claude_calls

    # Default to human-calls in project root
    return calls_dir


@app.callback(invoke_without_command=True)
def archive_calls(
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Preview what would be archived without moving files"),
    days: int = typer.Option(30, "--days", "-d", help="Archive calls completed more than N days ago"),
    all_completed: bool = typer.Option(False, "--all-completed", "-a", help="Archive all completed calls regardless of age"),
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
    show_stats: bool = typer.Option(False, "--stats", "-s", help="Show archive statistics after archiving"),
):
    """Archive completed/expired human calls to the archive directory.

    By default, archives calls completed more than 30 days ago.
    Archived calls are moved to human-calls/archive/.

    Examples:
        se3 archive-calls                    # Archive calls > 30 days old
        se3 archive-calls --days 7           # Archive calls > 7 days old
        se3 archive-calls --dry-run          # Preview what would be archived
        se3 archive-calls --all-completed    # Archive all completed calls
        se3 archive-calls --stats            # Show statistics after archiving
    """
    root = Path(project_root).resolve()
    calls_dir = _get_calls_dir(root)

    if not calls_dir.exists():
        typer.echo(
            typer.style(
                f"Error: Human calls directory not found at {calls_dir}",
                fg=typer.colors.RED,
            )
        )
        raise typer.Exit(1)

    store = HumanCallStore(calls_dir)

    # Show current archive stats if requested
    if show_stats:
        stats = store.get_archive_stats()
        typer.echo(typer.style("Current Archive Statistics:", fg=typer.colors.BLUE, bold=True))
        typer.echo(f"  Total archived: {stats['total_archived']}")
        if stats['by_status']['completed'] > 0:
            typer.echo(f"  Completed: {stats['by_status']['completed']}")
        if stats['by_status']['expired'] > 0:
            typer.echo(f"  Expired: {stats['by_status']['expired']}")
        if stats['oldest_archive']:
            typer.echo(f"  Oldest: {stats['oldest_archive'][:10]}")
        typer.echo()

    # Determine what to archive
    if all_completed:
        typer.echo(typer.style("Archiving all completed/expired calls...", fg=typer.colors.YELLOW))
    else:
        typer.echo(typer.style(f"Archiving calls completed more than {days} days ago...", fg=typer.colors.YELLOW))

    if dry_run:
        typer.echo(typer.style("[DRY RUN] No files will be moved", fg=typer.colors.CYAN))

    # Perform archiving
    archived = store.archive_completed_calls(
        days_old=days,
        dry_run=dry_run,
        all_completed=all_completed
    )

    if not archived:
        typer.echo(typer.style("No calls to archive.", fg=typer.colors.GREEN))
        raise typer.Exit(0)

    # Display results
    typer.echo()
    if dry_run:
        typer.echo(typer.style(f"Would archive {len(archived)} call(s):", fg=typer.colors.CYAN))
    else:
        typer.echo(typer.style(f"Archived {len(archived)} call(s):", fg=typer.colors.GREEN))

    for info in archived:
        status_color = typer.colors.GREEN if info['status'] == 'completed' else typer.colors.YELLOW
        typer.echo(f"  • {info['title'][:50]}")
        typer.echo(f"    Status: {typer.style(info['status'], fg=status_color)}")
        if dry_run:
            typer.echo(f"    Would move: {Path(info['file_path']).name}")
        else:
            typer.echo(f"    Archived to: {Path(info['archive_path']).name}")
        typer.echo()

    # Show updated stats if archiving actually happened
    if not dry_run and show_stats:
        stats = store.get_archive_stats()
        typer.echo(typer.style("Updated Archive Statistics:", fg=typer.colors.BLUE, bold=True))
        typer.echo(f"  Total archived: {stats['total_archived']}")
        typer.echo(f"  Archive location: {stats['archive_dir']}")

    raise typer.Exit(0)


@app.command(name="stats")
def archive_stats(
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
):
    """Show archive statistics without archiving anything."""
    root = Path(project_root).resolve()
    calls_dir = _get_calls_dir(root)

    if not calls_dir.exists():
        typer.echo(
            typer.style(
                f"Error: Human calls directory not found at {calls_dir}",
                fg=typer.colors.RED,
            )
        )
        raise typer.Exit(1)

    store = HumanCallStore(calls_dir)
    stats = store.get_archive_stats()

    typer.echo(typer.style("Archive Statistics", fg=typer.colors.BLUE, bold=True))
    typer.echo(f"  Archive directory: {stats['archive_dir']}")
    typer.echo(f"  Total archived calls: {typer.style(str(stats['total_archived']), fg=typer.colors.WHITE, bold=True)}")

    if stats['total_archived'] > 0:
        typer.echo()
        typer.echo("  By status:")
        for status, count in stats['by_status'].items():
            if count > 0:
                color = typer.colors.GREEN if status == 'completed' else typer.colors.YELLOW if status == 'expired' else typer.colors.WHITE
                typer.echo(f"    {status.capitalize()}: {typer.style(str(count), fg=color)}")

        if stats['by_month']:
            typer.echo()
            typer.echo("  By month:")
            for month, count in sorted(stats['by_month'].items()):
                typer.echo(f"    {month}: {count}")

        typer.echo()
        typer.echo(f"  Oldest archive: {stats['oldest_archive'][:10] if stats['oldest_archive'] else 'N/A'}")
        typer.echo(f"  Newest archive: {stats['newest_archive'][:10] if stats['newest_archive'] else 'N/A'}")

    raise typer.Exit(0)


@app.command(name="restore")
def restore_call(
    call_id: str = typer.Argument(..., help="ID of the call to restore from archive"),
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
):
    """Restore a call from archive back to active calls."""
    root = Path(project_root).resolve()
    calls_dir = _get_calls_dir(root)

    if not calls_dir.exists():
        typer.echo(
            typer.style(
                f"Error: Human calls directory not found at {calls_dir}",
                fg=typer.colors.RED,
            )
        )
        raise typer.Exit(1)

    store = HumanCallStore(calls_dir)

    # Try to restore the call
    restored = store.restore_from_archive(call_id)

    if restored:
        typer.echo(
            typer.style(
                f"Successfully restored call: {restored.title}",
                fg=typer.colors.GREEN,
            )
        )
        typer.echo(f"  ID: {restored.id}")
        typer.echo(f"  Status: {restored.status.value}")
        typer.echo(f"  File: {restored.file_path.name}")
    else:
        typer.echo(
            typer.style(
                f"No archived call found with ID containing: {call_id}",
                fg=typer.colors.RED,
            )
        )
        raise typer.Exit(1)

    raise typer.Exit(0)
