"""SE 3.0 CLI - Main entry point for se3 commands."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer

from . import __version__


def get_framework_version() -> str:
    """Get framework version from single source of truth (pyproject.toml via __init__.py)."""
    return __version__


from .commands import lint, status, verify, collab, commit, human_calls_cmd, human_input, work, health, run, dashboard, summary, init_cmd, history

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


# Register sub-typer commands (complex multi-command tools)
app.add_typer(lint.app, name="lint", help="Lint OpenSpec files")
app.add_typer(status.app, name="status", help="Check project status")
app.add_typer(health.app, name="health", help="Check OpenSpec system health and integrity")
app.add_typer(verify.app, name="verify", help="Verify spec coverage")
app.add_typer(collab.app, name="collab", help="Manage git-worktree multi-agent collaboration")
app.add_typer(commit.app, name="commit", help="Commit changes with SE3 verification")
app.add_typer(human_calls_cmd.app, name="human-calls", help="Manage human calls")
app.add_typer(human_input.app, name="human", help="Manage human input")
# run is registered as a direct command below (not sub-typer) to avoid positional arg + options parsing issues
app.add_typer(dashboard.app, name="dashboard", help="Display project status dashboard")
app.add_typer(summary.app, name="summary", help="Generate project context summary")
app.add_typer(init_cmd.app, name="init", help="Initialize SE3 project structure and base spec")
app.add_typer(history.app, name="history", help="Browse LLM chat history")

# Register direct commands (simple single-command tools with positional args)
# These are registered as direct commands to avoid sub-typer nesting issues
@app.command(name="start")
def start_cmd(
    format: str = typer.Option("text", "--format", "-f", help="Output format (text or json)"),
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
    input: Optional[str] = typer.Option(None, "--input", "-i", help="User input for intent classification"),
):
    """Start an SE3 session — compute state and return actions for the agent.

    [DEPRECATED] Use 'se3 run' instead. This command will be removed in SE3 3.0.

    Includes Input Classification & Stage Routing (SE3 1.x feature).
    """
    _show_deprecation_warning("start", "se3 run")
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
    strict: bool = typer.Option(False, "--strict", "-s", help="Enforce naming conventions strictly (reject invalid names)"),
):
    """Start or continue working on a change — the SDD workflow driver.

    [DEPRECATED] Use 'se3 run' instead. This command will be removed in SE3 3.0.
    """
    _show_deprecation_warning("work", "se3 run")
    from .commands.work import run_work, print_text_report, print_json_report
    result = run_work(project_root, change_name, new, advance, strict)

    # Check for session guard error
    if "error" in result:
        typer.echo(f"Error: {result['error']}", err=True)
        typer.echo(result.get("message", ""), err=True)
        if result.get("actions"):
            typer.echo("\nSuggested actions:", err=True)
            for action in result["actions"]:
                typer.echo(f"  - {action.get('type')}: {action.get('reason', '')}", err=True)
        raise typer.Exit(code=1)

    if format == "json":
        print_json_report(result)
    else:
        print_text_report(result)
    raise typer.Exit(code=0 if result.get("change") else 1)


# Register guardrails as a separate command (not under work due to Typer limitations)
# Usage: se3 guardrails <spec-file>
@app.command(name="guardrails")
def guardrails_cmd(
    spec_file: Path = typer.Argument(..., help="Path to spec file to check"),
    original: Optional[Path] = typer.Option(None, "--original", "-o", help="Path to original spec file for comparison"),
):
    """Check spec file against SE3 Spec Guardrails.

    [DEPRECATED] This functionality is now integrated into the flow engine.
    Use 'se3 run' which includes automatic spec verification.

    Verifies that spec requirements were not inappropriately
    weakened or deleted.
    """
    _show_deprecation_warning("guardrails", "se3 run (includes automatic verify-spec)")
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
            typer.echo(f"Warning: Could not find original version in git history")
            typer.echo(f"         Run from a git repository or use --original to specify the original file")
            original_content = new_content  # Compare with itself (no violations)

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
    archive: bool = typer.Option(False, "--archive", "-a", help="Automatically archive all completed changes"),
):
    """End an SE3 session — compute shutdown actions for the agent.

    [DEPRECATED] Use 'se3 run' instead. This command will be removed in SE3 3.0.
    """
    _show_deprecation_warning("done", "se3 run")
    from .commands.done import run_session_done, print_text_report, print_json_report
    state = run_session_done(project_root, auto_archive=archive)

    # Check for session guard error
    if "error" in state:
        typer.echo(f"Error: {state['error']}", err=True)
        typer.echo(state.get("message", ""), err=True)
        if state.get("actions"):
            typer.echo("\nSuggested actions:", err=True)
            for action in state["actions"]:
                typer.echo(f"  - {action.get('type')}: {action.get('reason', '')}", err=True)
        raise typer.Exit(code=1)

    if format == "json":
        print_json_report(state)
    else:
        print_text_report(state)
    raise typer.Exit(code=0)

def _show_deprecation_warning(old_cmd: str, new_cmd: str):
    """Show deprecation warning for legacy 2.x commands."""
    import warnings
    warnings.warn(
        f"'{old_cmd}' is deprecated and will be removed in SE3 3.0. Use '{new_cmd}' instead.",
        DeprecationWarning,
        stacklevel=3
    )
    # Also print to stderr for immediate visibility
    print(
        f"⚠️  WARNING: '{old_cmd}' is deprecated and will be removed in SE3 3.0.",
        file=sys.stderr
    )
    print(f"   Use '{new_cmd}' instead.\n", file=sys.stderr)


def _full_cycle_cmd_impl(
    description: str,
    project_root: str,
    quick: bool,
    format: str,
):
    """Implementation of full-cycle command."""
    _show_deprecation_warning("full-cycle", "se3 run")
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
    prompt: Optional[str] = typer.Argument(None, help="Description of work for each iteration"),
    iterations: int = typer.Option(10, "--iterations", "-n", help="Number of iterations (default: 10)"),
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
    quick: bool = typer.Option(False, "--quick", "-q", help="Quick mode - use 'small' workflow"),
    no_summary: bool = typer.Option(False, "--no-summary", help="Disable iteration summary between loops"),
    collab: bool = typer.Option(False, "--collab", "-c", help="Use collab mode for each iteration (parallel workers)"),
    mock: bool = typer.Option(False, "--mock", "-m", help="Mock mode for testing (simulates execution)"),
    auto: bool = typer.Option(False, "--auto", "-a", help="Auto mode - skip interactive prompts (for non-interactive environments)"),
    merge_branch: Optional[str] = typer.Option(None, "--merge", help="Merge a loop branch back to original branch"),
):
    """Run SE3 workflow in a loop, auto-executing all iterations.

    [DEPRECATED] Use 'se3 run --loop' instead. This command will be removed in SE3 3.0.

    Takes over the terminal, generates a bash while-loop script,
    and executes Claude Code for each iteration automatically.
    Press Ctrl+C to stop at any time.

    Each loop session creates its own branch (se3-loop/{timestamp}) for isolation.
    When finished, use --merge to integrate changes back to the original branch.

    Examples:
        se3 loop "refactor module" --iterations 5
        se3 loop "add test case" -n 20 --quick
        se3 loop "process item"                    # Default 10 iterations
        se3 loop "process item" --no-summary       # Disable summary between iterations
        se3 loop "optimize code" --collab -n 3     # Use collab mode with parallel workers
        se3 loop "test" --collab --mock -n 2       # Test collab mode with mock execution
        se3 loop "work" --collab --auto -n 3       # Auto mode (non-interactive)
        se3 loop --merge se3-loop/1234567890       # Merge loop branch back
    """
    _show_deprecation_warning("loop", "se3 run --loop")
    from .commands.loop import run_exclusive_loop, run_loop_collab, merge_loop_branch, get_current_branch, get_loop_branch_base, infer_loop_branch_base

    # Handle merge mode (prompt not required)
    if merge_branch:
        root = Path(project_root).resolve()

        # Validate that the merge branch exists
        result = subprocess.run(
            ["git", "rev-parse", "--verify", merge_branch],
            cwd=root,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            typer.echo(f"Error: Branch '{merge_branch}' not found", err=True)
            raise typer.Exit(code=1)

        # Get the base branch (original branch when loop was started)
        # First try to get it from git config, then try to infer from history
        base_branch = get_loop_branch_base(root, merge_branch)
        if not base_branch:
            # Try to infer from git history
            base_branch = infer_loop_branch_base(root, merge_branch)
        if not base_branch:
            # Last resort: use current branch
            base_branch = get_current_branch(root)
            typer.echo(f"Warning: Could not determine original base branch for {merge_branch}", err=True)
            typer.echo(f"Using current branch '{base_branch}' as merge target.", err=True)
            typer.echo(f"To specify a different target branch, switch to it first.", err=True)
            typer.echo("")
        else:
            typer.echo(f"Detected base branch: {base_branch}")

        success = merge_loop_branch(root, merge_branch, base_branch)
        raise typer.Exit(code=0 if success else 1)

    # Validate prompt is provided for normal loop execution
    if not prompt:
        typer.echo("Error: PROMPT is required when not using --merge", err=True)
        raise typer.Exit(code=1)

    if collab:
        run_loop_collab(prompt, project_root, iterations, quick, no_summary, mock, auto)
    else:
        run_exclusive_loop(prompt, project_root, iterations, quick, no_summary)
    raise typer.Exit(code=0)


@app.command(name="run")
def run_cmd(
    task: Optional[str] = typer.Argument(None, help="Task description"),
    resume: bool = typer.Option(False, "--resume", "-r", help="Resume interrupted flow"),
    loop: bool = typer.Option(False, "--loop", "-l", help="Loop mode (continuous task execution)"),
    type: str = typer.Option("feature", "--type", "-t", help="Task type (feature, bugfix, refactor, etc.)"),
    change: Optional[str] = typer.Option(None, "--change", "-c", help="Change name for this task"),
    flow_id: Optional[str] = typer.Option(None, "--flow-id", help="Specific flow ID to resume"),
):
    """SE3 Run — Unified entry point for the flow engine.

    Examples:
        se3 run "Implement user authentication"
        se3 run "Fix login bug" --type=bugfix
        se3 run --resume
        se3 run --loop
    """
    from .commands.run import run_flow, run_loop_mode, get_project_root, handle_resume_interactive, SE3_DIR

    project_root = get_project_root()

    # Ensure se3 directory exists
    se3_dir = project_root / SE3_DIR
    se3_dir.mkdir(exist_ok=True)
    (se3_dir / "state").mkdir(exist_ok=True)
    (se3_dir / "cache").mkdir(exist_ok=True)

    if loop:
        exit_code = run_loop_mode(
            project_root=project_root,
            initial_task=task,
            task_type=type,
        )
        raise typer.Exit(exit_code)

    if resume or flow_id:
        target_flow_id = flow_id

        if not target_flow_id:
            target_flow_id = handle_resume_interactive(project_root)

        if target_flow_id:
            exit_code = run_flow(
                project_root=project_root,
                flow_id=target_flow_id,
            )
            raise typer.Exit(exit_code)
        else:
            if not task:
                print("Error: Task description required for new flow", file=sys.stderr)
                raise typer.Exit(1)

    # New flow mode
    if not task:
        print("Error: Task description required (or use --resume)", file=sys.stderr)
        print("\nExamples:", file=sys.stderr)
        print('  se3 run "Implement feature X"', file=sys.stderr)
        print("  se3 run --resume", file=sys.stderr)
        raise typer.Exit(1)

    exit_code = run_flow(
        project_root=project_root,
        task_description=task,
        task_type=type,
        change_name=change,
        is_loop_mode=False,
    )
    raise typer.Exit(exit_code)


# Register handoff as a direct command (not a sub-typer)
@app.command(name="handoff")
def handoff_cmd(
    message: Optional[str] = typer.Argument(None, help="Handoff message describing what was done"),
    project_root: Optional[str] = typer.Option(None, "--project-root", "-p", help="Project root directory"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview what would happen without executing"),
    skip_commit: bool = typer.Option(False, "--skip-commit", help="Skip automatic commit (use with caution)"),
):
    """Handoff control to human — enforcing SE3 commit-before-handoff rule.

    [DEPRECATED] Use 'se3 run' which includes automatic commit and summarize steps.
    """
    _show_deprecation_warning("handoff", "se3 run (includes commit and summarize steps)")
    from .commands.handoff import handoff
    handoff(message=message, project_root=project_root, dry_run=dry_run, skip_commit=skip_commit)


@app.command(name="migrate")
def migrate_cmd(
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Show what would be migrated without making changes"),
    force: bool = typer.Option(False, "--force", "-f", help="Proceed even if se3/ already exists (merge mode)"),
):
    """Migrate legacy directory structure to new se3/ format.

    Moves directories from legacy locations to the new consolidated se3/ structure:
    - .se3/ → se3/ (state, logs, cache)
    - specs/ → se3/specs/
    - se3.config.yaml → se3.yaml

    Examples:
        se3 migrate                    # Perform migration
        se3 migrate --dry-run          # Preview changes
        se3 migrate --force            # Merge with existing se3/
    """
    from .commands.migrate import run_migration
    run_migration(project_root, dry_run, force)


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
