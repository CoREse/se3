"""Output sync command for SE 3.0 tools.

Synchronizes output/ directory with source files.
"""

from pathlib import Path
from typing import List, Tuple, Optional
import typer

from ..utils import get_file_mtime, copy_file, get_source_mappings, discover_outputs

app = typer.Typer()


class SyncResult:
    """Result of a sync operation."""

    def __init__(self):
        self.to_create: List[Tuple[Path, Path]] = []  # (src, dst)
        self.to_update: List[Tuple[Path, Path]] = []  # (src, dst)
        self.up_to_date: List[Path] = []
        self.orphaned: List[Path] = []


def analyze_sync(project_root: Path) -> SyncResult:
    """Analyze what needs to be synced.

    Args:
        project_root: Root of the SE 3.0 project

    Returns:
        SyncResult with categorized files
    """
    result = SyncResult()
    mappings = get_source_mappings(project_root)
    output_dir = project_root / "output"

    # Check each source-to-output mapping
    for output_path, source_path in mappings.items():
        source_mtime = get_file_mtime(source_path)
        output_mtime = get_file_mtime(output_path)

        if output_mtime is None:
            # Output doesn't exist - needs creation
            result.to_create.append((source_path, output_path))
        elif source_mtime and source_mtime > output_mtime:
            # Source is newer - needs update
            result.to_update.append((source_path, output_path))
        else:
            # Up to date
            result.up_to_date.append(output_path)

    # Find orphaned files (in output but no source mapping)
    if output_dir.exists():
        output_files = set(discover_outputs(output_dir))
        expected_outputs = set(mappings.keys())
        for orphan in output_files - expected_outputs:
            result.orphaned.append(orphan)

    return result


@app.command()
def sync(
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--apply",
        help="Preview changes (default) or apply them",
    ),
    prune: bool = typer.Option(
        False,
        "--prune",
        help="Remove orphaned output files",
    ),
    project_root: Optional[Path] = typer.Option(
        None,
        "--project-root",
        help="Project root directory (default: current directory)",
    ),
):
    """Synchronize output/ directory with source files."""

    if project_root is None:
        project_root = Path.cwd()
    else:
        project_root = Path(project_root).resolve()

    result = analyze_sync(project_root)

    # Report findings
    if result.to_create:
        typer.echo("Files to create:")
        for src, dst in result.to_create:
            typer.echo(f"  [CREATE] {src.name} -> {dst.relative_to(project_root)}")

    if result.to_update:
        typer.echo("Files to update:")
        for src, dst in result.to_update:
            typer.echo(f"  [UPDATE] {src.name} -> {dst.relative_to(project_root)}")

    if result.up_to_date:
        typer.echo("Files up to date:")
        for path in result.up_to_date:
            typer.echo(f"  [OK] {path.relative_to(project_root)}")

    if result.orphaned:
        if prune and not dry_run:
            typer.echo("Removing orphaned files:")
            for path in result.orphaned:
                typer.echo(f"  [REMOVED] {path.relative_to(project_root)}")
                path.unlink()
        else:
            typer.echo("Orphaned files (use --prune to remove):")
            for path in result.orphaned:
                typer.echo(f"  [ORPHAN] {path.relative_to(project_root)}")

    # Apply changes if requested
    if not dry_run:
        for src, dst in result.to_create + result.to_update:
            copy_file(src, dst)

        total_changes = len(result.to_create) + len(result.to_update)
        if total_changes > 0:
            typer.echo(f"\nApplied {total_changes} change(s).")
        else:
            typer.echo("\nNo changes needed.")
    else:
        total_changes = len(result.to_create) + len(result.to_update)
        if total_changes > 0:
            typer.echo(f"\n{total_changes} change(s) pending. Use --apply to apply.")
        else:
            typer.echo("\nOutput directory is in sync.")


if __name__ == "__main__":
    app()
