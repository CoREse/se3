"""OpenSpec CLI commands for SE 3.0.

Provides commands for managing the openspec/ directory structure:
- openspec init: Initialize the openspec/ directory structure
- openspec list --specs: List all available specs
- openspec archive: Archive a completed change
"""

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

import typer

from ..utils import parse_spec

app = typer.Typer(name="openspec", help="Manage OpenSpec directory and specs")


def initialize_openspec(project_root: Path, force: bool = False) -> Dict[str, Any]:
    """
    Initialize the openspec/ directory structure.

    Args:
        project_root: Root directory of the project
        force: Reinitialize even if already exists

    Returns:
        Dict with created directories and status
    """
    result = {
        "created": [],
        "existing": [],
        "success": True,
        "message": "",
    }

    openspec_dir = project_root / "openspec"

    # Check if already initialized
    if openspec_dir.exists() and not force:
        # Check if all required subdirectories exist
        required_dirs = ["specs", "changes", "changes/archive"]
        all_exist = all((openspec_dir / d).exists() for d in required_dirs)

        if all_exist:
            result["message"] = "OpenSpec directory already initialized. Use --force to reinitialize."
            result["existing"] = [str(openspec_dir / d) for d in required_dirs]
            return result

    # Create directory structure
    dirs_to_create = [
        openspec_dir / "specs",
        openspec_dir / "changes",
        openspec_dir / "changes" / "archive",
    ]

    for dir_path in dirs_to_create:
        if dir_path.exists():
            result["existing"].append(str(dir_path))
        else:
            dir_path.mkdir(parents=True, exist_ok=True)
            result["created"].append(str(dir_path))

    result["message"] = f"OpenSpec initialized: {len(result['created'])} directories created"
    return result


def list_specs(project_root: Path) -> List[Dict[str, Any]]:
    """
    List all available specs in openspec/specs/.

    Args:
        project_root: Root directory of the project

    Returns:
        List of spec dicts with name, path, and purpose
    """
    specs = []
    specs_dir = project_root / "openspec" / "specs"

    if not specs_dir.exists():
        return specs

    for spec_dir in sorted(specs_dir.iterdir()):
        if not spec_dir.is_dir():
            continue

        spec_file = spec_dir / "spec.md"
        if not spec_file.exists():
            continue

        spec_info = {
            "name": spec_dir.name,
            "path": str(spec_file.relative_to(project_root)),
            "purpose": None,
        }

        # Try to parse the spec file to extract purpose
        try:
            parsed = parse_spec(str(spec_file))
            if parsed.get("purpose"):
                spec_info["purpose"] = parsed["purpose"]
        except Exception:
            pass

        specs.append(spec_info)

    return specs


@app.callback()
def openspec_callback(ctx: typer.Context):
    """Manage OpenSpec directory and specs."""
    pass


@app.command(name="init")
def init_cmd(
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Reinitialize even if already initialized",
    ),
    project_root: str = typer.Option(
        ".",
        "--project-root",
        "-p",
        help="Root directory of the project",
    ),
    format: str = typer.Option(
        "text",
        "--format",
        "-f",
        help="Output format (text or json)",
    ),
):
    """Initialize the openspec/ directory structure.

    Creates the following directories:
    - openspec/specs/        # Source of truth for requirements
    - openspec/changes/      # Active changes
    - openspec/changes/archive/  # Archived changes
    """
    root = Path(project_root).resolve()
    result = initialize_openspec(root, force)

    if format == "json":
        typer.echo(json.dumps(result, indent=2, default=str))
    else:
        if result["created"]:
            typer.echo(typer.style("Created directories:", fg=typer.colors.GREEN))
            for d in result["created"]:
                typer.echo(f"  - {d}")

        if result["existing"]:
            typer.echo(typer.style("Existing directories:", fg=typer.colors.YELLOW))
            for d in result["existing"]:
                typer.echo(f"  - {d}")

        typer.echo(f"\n{result['message']}")

    raise typer.Exit(code=0 if result["success"] else 1)


@app.command(name="list")
def list_cmd(
    specs: bool = typer.Option(
        False,
        "--specs",
        "-s",
        help="List all available specs",
    ),
    project_root: str = typer.Option(
        ".",
        "--project-root",
        "-p",
        help="Root directory of the project",
    ),
    format: str = typer.Option(
        "text",
        "--format",
        "-f",
        help="Output format (text or json)",
    ),
):
    """List OpenSpec resources.

    Use --specs to list all available specs in openspec/specs/.
    """
    root = Path(project_root).resolve()

    if not specs:
        # Default: show help
        typer.echo(ctx.get_help())
        raise typer.Exit()

    # List specs
    spec_list = list_specs(root)

    if format == "json":
        typer.echo(json.dumps(spec_list, indent=2, default=str))
    else:
        if not spec_list:
            typer.echo(typer.style("No specs found in openspec/specs/", fg=typer.colors.YELLOW))
            raise typer.Exit(0)

        typer.echo(f"\n{'=' * 60}")
        typer.echo("Available Specs")
        typer.echo(f"{'=' * 60}\n")

        for spec in spec_list:
            typer.echo(typer.style(f"  {spec['name']}", fg=typer.colors.GREEN, bold=True))
            typer.echo(f"  Path: {spec['path']}")
            if spec['purpose']:
                # Truncate purpose if too long
                purpose = spec['purpose']
                if len(purpose) > 100:
                    purpose = purpose[:97] + "..."
                typer.echo(f"  Purpose: {purpose}")
            typer.echo()

        typer.echo(f"Total: {len(spec_list)} spec(s)")

    raise typer.Exit(code=0)


def archive_change(change_name: str, project_root: Path) -> Dict[str, Any]:
    """Archive a completed change by moving it to openspec/changes/archive/.

    Args:
        change_name: Name of the change to archive
        project_root: Root directory of the project

    Returns:
        Dict with archive operation results
    """
    result = {
        "change": change_name,
        "success": False,
        "source": None,
        "destination": None,
        "message": "",
    }

    changes_dir = project_root / "openspec" / "changes"
    archive_dir = changes_dir / "archive"

    # Handle nested change names (e.g., "feature/auth")
    source_path = changes_dir / change_name
    dest_path = archive_dir / change_name

    if not source_path.exists():
        result["message"] = f"Change '{change_name}' not found in openspec/changes/"
        return result

    # Create archive directory if needed
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Create parent directories in archive if needed (for nested changes)
    if "/" in change_name or "\\" in change_name:
        dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Check if destination already exists
    if dest_path.exists():
        # Append timestamp to avoid collision
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest_path = archive_dir / f"{change_name}-{timestamp}"
        result["message"] = f"Destination already existed, archived as {dest_path.name}"

    try:
        # Move the change directory
        shutil.move(str(source_path), str(dest_path))
        result["success"] = True
        result["source"] = str(source_path.relative_to(project_root))
        result["destination"] = str(dest_path.relative_to(project_root))
        if not result["message"]:
            result["message"] = f"Change '{change_name}' archived successfully"
    except Exception as e:
        result["message"] = f"Failed to archive change: {e}"

    return result


@app.command(name="archive")
def archive_cmd(
    change_name: str = typer.Argument(..., help="Name of the change to archive"),
    project_root: str = typer.Option(
        ".",
        "--project-root",
        "-p",
        help="Root directory of the project",
    ),
    format: str = typer.Option(
        "text",
        "--format",
        "-f",
        help="Output format (text or json)",
    ),
):
    """Archive a completed change to openspec/changes/archive/.

    Moves the change directory from openspec/changes/ to openspec/changes/archive/.
    This should be done after all tasks are complete and specs have been verified.

    Examples:
        openspec archive feature/auth
        openspec archive bugfix/login-fix --format json
    """
    root = Path(project_root).resolve()
    result = archive_change(change_name, root)

    if format == "json":
        typer.echo(json.dumps(result, indent=2, default=str))
    else:
        typer.echo(f"\n{'=' * 60}")
        typer.echo("OpenSpec Archive")
        typer.echo(f"{'=' * 60}\n")

        if result["success"]:
            typer.echo(typer.style(f"  ✓ {result['message']}", fg=typer.colors.GREEN))
            typer.echo(f"\n  Source: {result['source']}")
            typer.echo(f"  Destination: {result['destination']}")
        else:
            typer.echo(typer.style(f"  ✗ {result['message']}", fg=typer.colors.RED))

        typer.echo(f"\n{'=' * 60}")

    raise typer.Exit(code=0 if result["success"] else 1)
