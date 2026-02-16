"""SE 3.0 CLI - Main entry point for se3 commands."""

import json
from pathlib import Path
from typing import Optional

import typer

from . import __version__

def get_framework_version() -> str:
    """Get current framework version from single source of truth (direct file read for git worktree compatibility)."""
    import os
    from pathlib import Path
    import re

    # Get the path to __init__.py in the current working tree
    init_file = Path(__file__).parent / "__init__.py"
    if not init_file.exists():
        raise FileNotFoundError(f"Cannot find se3_tools __init__.py at {init_file}")

    with open(init_file, "r", encoding="utf-8") as f:
        content = f.read()

    match = re.search(r'SE3_FRAMEWORK_VERSION\s*=\s*"([\d]+\.[\d]+\.[\d]+)"', content)
    if not match:
        raise ValueError("Cannot find SE3_FRAMEWORK_VERSION definition in __init__.py")

    return match.group(1)
from .commands import init, lint, status, sync, verify, update, collab, commit
from .commands.update import get_installed_se3_version

app = typer.Typer(
    name="se3",
    help="SE 3.0 framework CLI tools",
    invoke_without_command=True,
)

def _version_callback(value: bool):
    """Handle --version flag."""
    if value:
        typer.echo(f"se3 CLI version: {__version__}")
        typer.echo(f"SE3 Framework version: {get_framework_version()}")
        try:
            installed = get_installed_se3_version()
            if installed != "0.0.0":
                typer.echo(f"Project SE3 version: {installed}")
        except Exception:
            pass
        raise typer.Exit()

@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", "-v", help="Show version information", callback=_version_callback, is_eager=True
    ),
):
    """SE 3.0 framework CLI tools."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


# Register commands
app.add_typer(init.app, name="init", help="Initialize a new SE 3.0 project")
app.add_typer(lint.app, name="lint", help="Lint OpenSpec files")
app.add_typer(status.app, name="status", help="Check project status")
app.add_typer(sync.app, name="sync", help="Sync output files")
app.add_typer(verify.app, name="verify", help="Verify spec coverage")
app.add_typer(update.app, name="update", help="Update SE 3.0 framework to latest version")
app.add_typer(collab.app, name="collab", help="Manage git-worktree multi-agent collaboration")
app.add_typer(commit.app, name="commit", help="Commit changes with SE3 verification")


@app.command(name="claude-cmd")
def claude_cmd(
    all_cmds: bool = typer.Option(False, "--all", help="Output all commands as JSON sorted by priority"),
    next_after: Optional[str] = typer.Option(None, "--next", help="Output the next command after the given one"),
    project_root: Optional[str] = typer.Option(None, "--project-root", "-p", help="Project root directory"),
):
    """Show configured Claude commands by priority.

    Used by bash scripts to resolve which Claude command to use.
    """
    from .config import load_claude_commands

    root = Path(project_root) if project_root else Path.cwd()
    commands = load_claude_commands(root)

    if all_cmds:
        typer.echo(json.dumps(commands))
    elif next_after:
        found = False
        for i, entry in enumerate(commands):
            if entry["cmd"] == next_after:
                if i + 1 < len(commands):
                    typer.echo(commands[i + 1]["cmd"])
                else:
                    typer.echo("")  # No next command
                found = True
                break
        if not found:
            typer.echo("")  # Command not in list
    else:
        # Default: output highest-priority command
        typer.echo(commands[0]["cmd"])


if __name__ == "__main__":
    app()
