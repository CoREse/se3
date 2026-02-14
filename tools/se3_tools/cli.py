"""SE 3.0 CLI - Main entry point for se3 tools.

Unified CLI for SE 3.0 framework tools.
"""

import sys
from pathlib import Path

import typer

from .commands.sync import sync
from .commands.lint import run_lint
from .commands.verify import main as verify_main
from .commands.status import main as status_main
from .commands.init import init

app = typer.Typer(help="SE 3.0 Framework Tools")

# Register commands
app.command(name="sync")(sync)
app.command(name="init")(init)


@app.callback()
def main():
    """SE 3.0 Framework Tools CLI.

    Tools for managing SE 3.0 projects:
    - init: Initialize a new SE 3.0 project
    - sync: Synchronize output/ directory with source files
    - lint: Validate spec files
    - verify: Check scenario coverage for a change
    - status: Run diagnostics on project status
    """
    pass


@app.command()
def lint(
    path: Path = typer.Argument(
        ".",
        help="Path to search for specs (default: current directory)",
        exists=True,
        file_okay=False,
        dir_okay=True,
    ),
) -> None:
    """Validate SE 3.0 spec files."""
    exit_code = run_lint(str(path))
    sys.exit(exit_code)


@app.command()
def verify(
    change: str = typer.Option(
        ...,
        "--change",
        "-c",
        help="Name of the change to verify",
    ),
    format: str = typer.Option(
        "text",
        "--format",
        "-f",
        help="Output format (text or json)",
    ),
) -> None:
    """Verify scenario coverage for a change."""
    exit_code = verify_main(change=change, format=format)
    sys.exit(exit_code)


@app.command()
def status(
    format: str = typer.Option(
        "text",
        "--format",
        "-f",
        help="Output format (text or json)",
    ),
) -> None:
    """Run diagnostics on project status."""
    exit_code = status_main(format=format)
    sys.exit(exit_code)


if __name__ == "__main__":
    app()
