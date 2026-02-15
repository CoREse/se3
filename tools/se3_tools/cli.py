"""SE 3.0 CLI - Main entry point for se3 commands."""

import typer

from .commands import init, lint, status, sync, verify, update, collab, commit

app = typer.Typer(
    name="se3",
    help="SE 3.0 framework CLI tools",
    no_args_is_help=True,
)

# Register commands
app.add_typer(init.app, name="init", help="Initialize a new SE 3.0 project")
app.add_typer(lint.app, name="lint", help="Lint OpenSpec files")
app.add_typer(status.app, name="status", help="Check project status")
app.add_typer(sync.app, name="sync", help="Sync output files")
app.add_typer(verify.app, name="verify", help="Verify spec coverage")
app.add_typer(update.app, name="update", help="Update SE 3.0 framework to latest version")
app.add_typer(collab.app, name="collab", help="Manage git-worktree multi-agent collaboration")
app.add_typer(commit.app, name="commit", help="Commit changes with SE3 verification")


if __name__ == "__main__":
    app()
