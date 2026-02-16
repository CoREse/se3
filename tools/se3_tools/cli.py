"""SE 3.0 CLI - Main entry point for se3 commands."""

import typer

from . import __version__, SE3_FRAMEWORK_VERSION
from .commands import init, lint, status, sync, verify, update, collab, commit
from .controller import daemon as controller_daemon
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
        typer.echo(f"SE3 Framework version: {SE3_FRAMEWORK_VERSION}")
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

# Register controller commands (external daemon)
app.command(name="daemon")(controller_daemon.daemon)
app.command(name="session")(controller_daemon.session)
app.command(name="collab-v2")(controller_daemon.collab)


if __name__ == "__main__":
    app()
