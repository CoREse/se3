"""SE3 init command — initialize project structure and base spec."""

import logging
from pathlib import Path

import typer

logger = logging.getLogger(__name__)

app = typer.Typer(help="Initialize SE3 project structure")

SE3_DIR = "se3"
SPECS_DIR = "specs"
BASE_SPEC_DIR = "base"
BASE_SPEC_FILE = "spec.md"

# Default se3.yaml content
DEFAULT_SE3_YAML = """\
# SE3 Project Configuration
# See: se3/specs/se3-config/spec.md

project:
  name: "{project_name}"
"""


def _get_template_content() -> str:
    """Load base spec template from package templates."""
    template_path = Path(__file__).parent.parent / "templates" / "base_spec.md"
    if not template_path.exists():
        raise FileNotFoundError(f"Base spec template not found: {template_path}")
    return template_path.read_text()


def run_init(project_root: Path, project_name: str) -> dict:
    """Run SE3 project initialization.

    Creates:
    - se3/specs/ directory
    - se3.yaml (if not exists)
    - se3/specs/base/spec.md (from template, if not exists)

    Args:
        project_root: Root directory of the project
        project_name: Name of the project

    Returns:
        Dict with created/skipped files
    """
    result = {"created": [], "skipped": [], "project_root": str(project_root)}

    # 1. Create se3/specs/ directory
    specs_dir = project_root / SE3_DIR / SPECS_DIR
    specs_dir.mkdir(parents=True, exist_ok=True)

    # 2. Create se3.yaml if not exists
    yaml_path = project_root / "se3.yaml"
    if not yaml_path.exists():
        yaml_content = DEFAULT_SE3_YAML.format(project_name=project_name)
        yaml_path.write_text(yaml_content)
        result["created"].append("se3.yaml")
        logger.info("Created se3.yaml")
    else:
        result["skipped"].append("se3.yaml (already exists)")
        logger.info("Skipped se3.yaml (already exists)")

    # 3. Generate se3/specs/base/spec.md from template
    base_dir = specs_dir / BASE_SPEC_DIR
    base_spec_path = base_dir / BASE_SPEC_FILE

    if not base_spec_path.exists():
        base_dir.mkdir(parents=True, exist_ok=True)
        template = _get_template_content()
        content = template.replace("{project_name}", project_name)
        base_spec_path.write_text(content)
        result["created"].append(f"{SE3_DIR}/{SPECS_DIR}/{BASE_SPEC_DIR}/{BASE_SPEC_FILE}")
        logger.info(f"Created base spec: {base_spec_path}")
    else:
        result["skipped"].append(f"{SE3_DIR}/{SPECS_DIR}/{BASE_SPEC_DIR}/{BASE_SPEC_FILE} (already exists)")
        logger.info(f"Skipped base spec (already exists): {base_spec_path}")

    return result


@app.callback(invoke_without_command=True)
def init(
    ctx: typer.Context,
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
    name: str = typer.Option("", "--name", "-n", help="Project name (default: directory name)"),
):
    """Initialize SE3 project structure and generate base spec.

    Creates the se3/ directory structure, se3.yaml config, and a base spec
    template in se3/specs/base/spec.md.

    Examples:
        se3 init
        se3 init --name "My Project"
        se3 init -p /path/to/project -n "SE3 Framework"
    """
    root = Path(project_root).resolve()

    if not name:
        name = root.name

    result = run_init(root, name)

    # Output results
    if result["created"]:
        typer.echo("Created:")
        for f in result["created"]:
            typer.echo(f"  + {f}")

    if result["skipped"]:
        typer.echo("Skipped:")
        for f in result["skipped"]:
            typer.echo(f"  - {f}")

    if not result["created"] and result["skipped"]:
        typer.echo("\nProject already initialized. No changes made.")
    elif result["created"]:
        typer.echo(f"\nSE3 project initialized in {root}")
        base_path = root / SE3_DIR / SPECS_DIR / BASE_SPEC_DIR / BASE_SPEC_FILE
        typer.echo(f"Edit {base_path} to fill in project-specific details.")
