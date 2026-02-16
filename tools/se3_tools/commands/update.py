"""SE 3.0 update command - Update existing SE 3.0 project to latest framework."""

import hashlib
import re
from datetime import datetime
from pathlib import Path
import typer

from ..utils import copy_file
def get_framework_version() -> str:
    """Get current framework version from single source of truth (direct file read for git worktree compatibility)."""
    import os
    from pathlib import Path

    # Get the path to __init__.py in the current working tree
    init_file = Path(__file__).parent.parent / "__init__.py"
    if not init_file.exists():
        raise FileNotFoundError(f"Cannot find se3_tools __init__.py at {init_file}")

    with open(init_file, "r", encoding="utf-8") as f:
        content = f.read()

    import re
    match = re.search(r'SE3_FRAMEWORK_VERSION\s*=\s*"([\d]+\.[\d]+\.[\d]+)"', content)
    if not match:
        raise ValueError("Cannot find SE3_FRAMEWORK_VERSION definition in __init__.py")

    return match.group(1)

app = typer.Typer()


def compute_checksum(content: str) -> str:
    """Compute SHA-256 checksum of content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def get_current_date() -> str:
    """Get current date in YYYY-MM-DD format."""
    return datetime.now().strftime("%Y-%m-%d")


def get_installed_se3_version() -> str:
    """Get currently installed SE3 version from .claude/SE3.md (direct file read for consistency)."""
    se3_path = Path(".claude/SE3.md")
    if not se3_path.exists():
        return "0.0.0"  # Not initialized

    content = se3_path.read_text(encoding="utf-8")
    # Parse version from metadata comment
    import re
    match = re.search(r'<!-- SE3 Version: ([\d]+\.[\d]+\.[\d]+) -->', content)
    if match:
        return match.group(1)
    # Fallback to old format parsing
    for line in content.split("\n")[:5]:
        if "SE3 Version:" in line:
            return line.split("SE3 Version:")[1].strip().rstrip(" -->")
    return "0.0.0"


def update_project(
    dry_run: bool = False,
    force: bool = False,
    se3_version: str = None,
) -> None:
    """
    Update an SE 3.0 project to the latest framework version.

    Args:
        dry_run: Show what would be updated without making changes
        force: Update even if already on latest version
        se3_version: Target SE 3.0 version
    """
    if se3_version is None:
        se3_version = get_framework_version()
    """
    Update an SE 3.0 project to the latest framework version.

    Args:
        dry_run: Show what would be updated without making changes
        force: Update even if already on latest version
        se3_version: Target SE 3.0 version
    """
    claude_dir = Path(".claude")

    # Check if project is initialized
    if not claude_dir.exists():
        typer.echo(
            typer.style(
                "Error: Project not initialized. Run 'se3 init' first.",
                fg=typer.colors.RED,
            )
        )
        raise typer.Exit(1)

    current_version = get_installed_se3_version()

    # Check if update is needed
    if current_version == se3_version and not force:
        typer.echo(
            typer.style(
                f"Already on SE3 version {se3_version}. Use --force to update anyway.",
                fg=typer.colors.GREEN,
            )
        )
        raise typer.Exit(0)

    # Locate project root and template directory (using same method as version.py for consistency)
    def find_project_root() -> Path:
        """Find project root by looking for .git (directory or file in worktrees)."""
        current = Path.cwd()
        while current != current.parent:
            git_path = current / ".git"
            if git_path.exists():
                return current
            current = current.parent
        return Path.cwd()

    project_root = find_project_root()
    templates_dir = project_root / "output"

    se3_template_path = templates_dir / "SE3.md.template"

    if not se3_template_path.exists():
        typer.echo(
            typer.style(
                f"Error: Template not found at {se3_template_path}",
                fg=typer.colors.RED,
            )
        )
        raise typer.Exit(1)

    # Read template
    se3_content = se3_template_path.read_text(encoding="utf-8")

    # Add metadata
    metadata = (
        f"<!-- Generated on {get_current_date()} -->\n"
        f"<!-- SE3 Version: {se3_version} -->\n"
        f"<!-- Checksum: {compute_checksum(se3_content)} -->\n\n"
    )
    se3_content = metadata + se3_content

    # Show what would be updated
    se3_path = claude_dir / "SE3.md"

    if dry_run:
        typer.echo("Would update:")
        typer.echo(f"  {se3_path}")
        typer.echo(f"  Version: {current_version} → {se3_version}")
        return

    # Perform update
    se3_path.write_text(se3_content, encoding="utf-8")

    typer.echo(
        typer.style(
            f"Updated SE3.md to version {se3_version}",
            fg=typer.colors.GREEN,
        )
    )
    if current_version != se3_version:
        typer.echo(f"  Previous version: {current_version}")



@app.command()
def update(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-n",
        help="Show what would be updated without making changes",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Update even if already on latest version",
    ),
    se3_version: str = typer.Option(
        None,
        "--se3-version",
        "-v",
        help="Target SE3 version to update to (format: MAJOR.MINOR.PATCH)",
    ),
) -> None:
    """Update existing SE 3.0 project to latest framework version."""
    try:
        if se3_version is None:
            se3_version = get_framework_version()
        update_project(dry_run, force, se3_version)
    except Exception as e:
        typer.echo(
            typer.style(
                f"Error during update: {str(e)}",
                fg=typer.colors.RED,
            )
        )
        raise typer.Exit(1)
