"""SE 3.0 CLI - Main entry point for se3 commands."""

import json
from pathlib import Path
from typing import Optional

import typer

from . import __version__, SE3_FRAMEWORK_VERSION

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
from .commands import lint, status, sync, verify, update, collab, commit, openspec, human_calls_cmd, human_input
from .commands.update import get_installed_se3_version
from .commands.init import initialize_project

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


# Register direct commands
@app.command(name="init")
def init_cmd(
    force: bool = typer.Option(False, "--force", "-f", help="Reinitialize even if already initialized"),
    offline: bool = typer.Option(False, "--offline", "-o", help="Use local templates without network"),
    with_config: bool = typer.Option(False, "--with-config", "-c", help="Create se3.config.yaml"),
    se3_version: str = typer.Option(SE3_FRAMEWORK_VERSION, "--se3-version", "-v", help="Specify SE3 version to use"),
):
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

# Register sub-typer commands (complex multi-command tools)
app.add_typer(lint.app, name="lint", help="Lint OpenSpec files")
app.add_typer(status.app, name="status", help="Check project status")
app.add_typer(sync.app, name="sync", help="Sync output files")
app.add_typer(verify.app, name="verify", help="Verify spec coverage")
app.add_typer(update.app, name="update", help="Update SE 3.0 framework to latest version")
app.add_typer(collab.app, name="collab", help="Manage git-worktree multi-agent collaboration")
app.add_typer(commit.app, name="commit", help="Commit changes with SE3 verification")
app.add_typer(openspec.app, name="openspec", help="Manage OpenSpec directory and specs")
app.add_typer(human_calls_cmd.app, name="human-calls", help="Manage human calls")
app.add_typer(human_input.app, name="human", help="Manage human input")

# Register direct commands (simple single-command tools with positional args)
# These are registered as direct commands to avoid sub-typer nesting issues
@app.command(name="start")
def start_cmd(
    format: str = typer.Option("text", "--format", "-f", help="Output format (text or json)"),
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
    input: Optional[str] = typer.Option(None, "--input", "-i", help="User input for intent classification"),
):
    """Start an SE3 session — compute state and return actions for the agent.

    Includes Input Classification & Stage Routing (SE3 1.x feature).
    """
    from .commands.start import run_session_start, print_text_report, print_json_report
    import json
    state = run_session_start(project_root, input)
    if format == "json":
        print_json_report(state)
    else:
        print_text_report(state)
    raise typer.Exit(code=0 if not state.get("actions") else 1)

@app.command(name="work")
def work_cmd(
    change_name: Optional[str] = typer.Argument(None, help="Name of the change to work on"),
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
    new: Optional[str] = typer.Option(None, "--new", help="Create new change with workflow type (bugfix/feature/review/directive)"),
    advance: bool = typer.Option(False, "--advance", "-a", help="Mark current step complete and advance to next"),
    format: str = typer.Option("text", "--format", "-f", help="Output format (text or json)"),
):
    """Start or continue working on a change — the SDD workflow driver."""
    from .commands.work import run_work, print_text_report, print_json_report
    result = run_work(project_root, change_name, new, advance)
    if format == "json":
        print_json_report(result)
    else:
        print_text_report(result)
    raise typer.Exit(code=0 if result.get("change") else 1)


@app.command(name="guardrails")
def guardrails_cmd(
    spec_file: Path = typer.Argument(..., help="Path to spec file to check"),
    original: Optional[Path] = typer.Option(None, "--original", "-o", help="Path to original spec file for comparison"),
):
    """Check spec file against SE3 Spec Guardrails.

    Verifies that spec requirements were not inappropriately
    weakened or deleted.
    """
    from .commands.work import check_spec_guardrails
    import subprocess
    import re

    if not spec_file.exists():
        typer.echo(f"Error: Spec file not found: {spec_file}", err=True)
        raise typer.Exit(code=1)

    new_content = spec_file.read_text()

    if original and original.exists():
        original_content = original.read_text()
    else:
        # Try to get original from git
        result = subprocess.run(
            ["git", "show", f"HEAD:{spec_file}"],
            capture_output=True, text=True, cwd=spec_file.parent
        )
        if result.returncode == 0:
            original_content = result.stdout
        else:
            typer.echo(f"Warning: Could not find original version for comparison")
            original_content = new_content

    violations = check_spec_guardrails(spec_file, original_content, new_content)

    typer.echo(f"\n{'=' * 60}")
    typer.echo("SE 3.0 Spec Guardrails Check")
    typer.echo(f"{'=' * 60}")
    typer.echo(f"\nFile: {spec_file}")

    if violations:
        typer.echo(f"\n⚠️  {len(violations)} violation(s) found:")
        for v in violations:
            typer.echo(f"\n  [{v['type']}] {v['message']}")
            typer.echo(f"  Rule: {v['guardrail']}")
        typer.echo(f"\n{'=' * 60}")
        raise typer.Exit(code=1)
    else:
        typer.echo(f"\n✓ All guardrails passed - no violations found")
        typer.echo(f"\n{'=' * 60}")
        raise typer.Exit(code=0)

@app.command(name="done")
def done_cmd(
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
    format: str = typer.Option("text", "--format", "-f", help="Output format (text or json)"),
):
    """End an SE3 session — compute shutdown actions for the agent."""
    from .commands.done import run_session_done, print_text_report, print_json_report
    state = run_session_done(project_root)
    if format == "json":
        print_json_report(state)
    else:
        print_text_report(state)
    raise typer.Exit(code=0)

def _full_cycle_cmd_impl(
    description: str,
    project_root: str,
    quick: bool,
    format: str,
):
    """Implementation of full-cycle command."""
    from .commands.fullcycle import run_full_cycle, print_text_report as fullcycle_print_text, print_json_report as fullcycle_print_json
    result = run_full_cycle(description, project_root, quick)
    if format == "json":
        fullcycle_print_json(result)
    else:
        fullcycle_print_text(result)
    raise typer.Exit(code=0 if result.get("success") else 1)


@app.command(name="full-cycle")
def full_cycle_cmd(
    description: str = typer.Argument(..., help="Description of the work to do"),
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
    quick: bool = typer.Option(False, "--quick", "-q", help="Quick mode - skip formal change creation, use 'small' workflow"),
    format: str = typer.Option("text", "--format", "-f", help="Output format (text or json)"),
):
    """Run the complete start-work-done workflow in one command.

    This command streamlines simple/quick tasks by combining:
    1. se3 start - Initialize the session
    2. se3 work --new - Create a change for the work
    3. Implementation (performed by agent)
    4. se3 done - Complete the session

    Examples:
        se3 full-cycle "fix login bug"
        se3 full-cycle "add user profile page" --quick
        se3 full-cycle "update documentation" -q --json
    """
    _full_cycle_cmd_impl(description, project_root, quick, format)


@app.command(name="fc")
def fc_cmd(
    description: str = typer.Argument(..., help="Description of the work to do"),
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
    quick: bool = typer.Option(False, "--quick", "-q", help="Quick mode - skip formal change creation, use 'small' workflow"),
    format: str = typer.Option("text", "--format", "-f", help="Output format (text or json)"),
):
    """Alias for 'full-cycle' - Run the complete start-work-done workflow in one command."""
    _full_cycle_cmd_impl(description, project_root, quick, format)


@app.command(name="loop")
def loop_cmd(
    prompt: str = typer.Argument(..., help="Description of work for each iteration"),
    iterations: int = typer.Option(10, "--iterations", "-n", help="Number of iterations (default: 10)"),
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
    quick: bool = typer.Option(False, "--quick", "-q", help="Quick mode - use 'small' workflow"),
    no_summary: bool = typer.Option(False, "--no-summary", help="Disable iteration summary between loops"),
):
    """Run SE3 workflow in a loop, auto-executing all iterations.

    Takes over the terminal, generates a bash while-loop script,
    and executes Claude Code for each iteration automatically.
    Press Ctrl+C to stop at any time.

    Examples:
        se3 loop "refactor module" --iterations 5
        se3 loop "add test case" -n 20 --quick
        se3 loop "process item"                    # Default 10 iterations
        se3 loop "process item" --no-summary       # Disable summary between iterations
    """
    from .commands.loop import run_exclusive_loop

    run_exclusive_loop(prompt, project_root, iterations, quick, no_summary)
    raise typer.Exit(code=0)


# Register handoff as a direct command (not a sub-typer)
@app.command(name="handoff")
def handoff_cmd(
    message: Optional[str] = typer.Argument(None, help="Handoff message describing what was done"),
    project_root: Optional[str] = typer.Option(None, "--project-root", "-p", help="Project root directory"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview what would happen without executing"),
    skip_commit: bool = typer.Option(False, "--skip-commit", help="Skip automatic commit (use with caution)"),
):
    """Handoff control to human — enforcing SE3 commit-before-handoff rule."""
    from .commands.handoff import handoff
    handoff(message=message, project_root=project_root, dry_run=dry_run, skip_commit=skip_commit)


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
