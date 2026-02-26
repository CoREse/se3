"""SE 3.0 init command - Initialize a new SE 3.0 project."""

# Verify: se3-scaffold/SE3.md Generation via se3 init
# Verify: se3-scaffold/New project adopts SE 3.0
# Verify: se3-scaffold/Project initialization

import os
import hashlib
import subprocess
from datetime import datetime
from pathlib import Path
import shutil
import typer

from ..utils import copy_file
from .. import SE3_FRAMEWORK_VERSION

app = typer.Typer()


def compute_checksum(content: str) -> str:
    """Compute SHA-256 checksum of content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def get_project_name() -> str:
    """Get project name from current directory."""
    return Path.cwd().name


def get_current_date() -> str:
    """Get current date in YYYY-MM-DD format."""
    return datetime.now().strftime("%Y-%m-%d")


def initialize_project(
    force: bool = False,
    offline: bool = False,
    with_config: bool = False,
    se3_version: str = SE3_FRAMEWORK_VERSION,
) -> None:
    """
    Initialize an SE 3.0 project.

    Args:
        force: Reinitialize even if already initialized
        offline: Use local templates without network
        with_config: Create se3.config.yaml
        se3_version: SE 3.0 version to use
    """
    # Check if already initialized
    claude_dir = Path(".claude")
    if claude_dir.exists():
        if not force:
            typer.echo(
                typer.style(
                    "Error: Project already initialized. Use --force to reinitialize.",
                    fg=typer.colors.RED,
                )
            )
            raise typer.Exit(1)
        # Force mode: only overwrite SE3.md and CLAUDE.md, preserve everything else
        typer.echo("Force mode: overwriting SE3.md and CLAUDE.md in .claude/")

    # Create .claude directory (no-op if it already exists)
    claude_dir.mkdir(parents=True, exist_ok=True)

    # 1. Create SE3.md (from template with version and metadata)
    # Locate template directory relative to package location
    project_root = Path(__file__).parent.parent.parent.parent
    templates_dir = project_root / "output"

    se3_template_path = templates_dir / "SE3.md.template"
    se3_content = se3_template_path.read_text(encoding="utf-8")

    # Add metadata
    metadata = (
        f"<!-- Generated on {get_current_date()} -->\n"
        f"<!-- SE3 Version: {se3_version} -->\n"
        f"<!-- Checksum: {compute_checksum(se3_content)} -->\n\n"
    )

    se3_content = metadata + se3_content

    se3_path = claude_dir / "SE3.md"
    se3_path.write_text(se3_content, encoding="utf-8")
    typer.echo(f"Created {se3_path}")

    # 2. Create CLAUDE.md from minimal template
    minimal_template_path = templates_dir / "CLAUDE.minimal.md.template"
    claude_content = minimal_template_path.read_text(encoding="utf-8")

    # Replace placeholders
    claude_content = claude_content.replace("{{PROJECT_NAME}}", get_project_name())

    claude_path = claude_dir / "CLAUDE.md"
    claude_path.write_text(claude_content, encoding="utf-8")
    typer.echo(f"Created {claude_path}")

    # 3. Create se3.config.yaml if --with-config flag
    if with_config:
        config_template_path = templates_dir / "se3.config.yaml"
        config_path = Path("se3.config.yaml")
        if config_template_path.exists():
            copy_file(config_template_path, config_path)
            typer.echo(f"Created {config_path}")
        else:
            typer.echo(
                typer.style(
                    f"Warning: Config template not found at {config_template_path}",
                    fg=typer.colors.YELLOW,
                )
            )

    # 4. Create se3/ directory structure (SE3 2.x+, VISIBLE not hidden)
    se3_dir = Path("se3")
    for subdir in ["calls/active", "calls/archive", "collab", "tmp", "state"]:
        (se3_dir / subdir).mkdir(parents=True, exist_ok=True)
    typer.echo(f"Created {se3_dir}/ directory structure (visible, not hidden)")

    # 5. Create output directory
    output_dir = Path("output")
    output_dir.mkdir(parents=True, exist_ok=True)

    # 5. Initialize specs directory
    typer.echo("Initializing specs directory...")
    specs_dir = Path("specs")
    specs_changelog = specs_dir / "_changelog"
    specs_dir.mkdir(parents=True, exist_ok=True)
    specs_changelog.mkdir(parents=True, exist_ok=True)
    typer.echo(typer.style("Specs directory initialized", fg=typer.colors.GREEN))

    # 6. Install SE3 workflow skills to .claude/commands/se3/
    skills_source = templates_dir / "commands" / "se3"
    skills_dest = claude_dir / "commands" / "se3"
    if skills_source.exists():
        skills_dest.mkdir(parents=True, exist_ok=True)
        for skill_file in skills_source.glob("*.md"):
            dest_file = skills_dest / skill_file.name
            shutil.copy2(skill_file, dest_file)
            typer.echo(f"Installed skill: {dest_file}")

    typer.echo(
        typer.style(
            "SE 3.0 project initialized successfully!",
            fg=typer.colors.GREEN,
        )
    )
    typer.echo("\nStart working with: /se3:start")


@app.command()
def init(
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Reinitialize even if already initialized",
    ),
    offline: bool = typer.Option(
        False,
        "--offline",
        "-o",
        help="Use local templates without network",
    ),
    with_config: bool = typer.Option(
        False,
        "--with-config",
        "-c",
        help="Create se3.config.yaml",
    ),
    se3_version: str = typer.Option(
        SE3_FRAMEWORK_VERSION,
        "--se3-version",
        "-v",
        help="Specify SE3 version to use",
    ),
) -> None:
    """Initialize a new SE 3.0 project."""
    try:
        initialize_project(force, offline, with_config, se3_version)
    except Exception as e:
        typer.echo(
            typer.style(
                f"Error during initialization: {str(e)}",
                fg=typer.colors.RED,
            )
        )
        raise typer.Exit(1)
