"""SE 3.0 CLI - Main entry point for se3 commands."""

import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer

from . import __version__

# Import display utilities early to ensure console is initialized
from .engine.display import get_console, render_full, render_text


app = typer.Typer(
    name="se3",
    help="SE 3.0 framework CLI tools",
    invoke_without_command=True,
)

def _version_callback(value: bool):
    """Handle --version flag."""
    if value:
        typer.echo(f"se3 version {__version__}")
        raise typer.Exit()

@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", "-v", help="Show version information", callback=_version_callback, is_eager=True
    ),
):
    """SE 3.0 framework CLI tools."""
    # Initialize display console at CLI startup
    _init_display()

    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


def _init_display() -> None:
    """Initialize display utilities for consistent output."""
    # Initialize the console (creates global console instance)
    console = get_console()

    # Log initialization for debugging
    import logging
    logger = logging.getLogger(__name__)
    logger.debug("Display utilities initialized")


def _read_multiline_input() -> Optional[str]:
    """Read multiline input from stdin with smart display for pasted content."""
    # Check if stdin is a tty (interactive terminal)
    if not sys.stdin.isatty():
        # Non-interactive mode (pipe/redirect): read all at once, show full content
        try:
            content = sys.stdin.read()
            lines = content.split("\n")

            # Show full content for all input (no truncation)
            if lines:
                render_full("\n".join(lines), title="Input")

            content = content.strip()
            return content if content else None
        except (EOFError, KeyboardInterrupt):
            return None

    # Interactive mode
    render_text("Enter task description (Ctrl+D to finish, Ctrl+C to cancel):", title="Input")

    lines = []
    is_paste_mode = False

    # Platform-specific paste detection
    def _has_pending_input() -> bool:
        """Check if there's more input immediately available."""
        try:
            if sys.platform == "win32":
                import msvcrt
                return msvcrt.kbhit()
            else:
                import select
                readable, _, _ = select.select([sys.stdin], [], [], 0.05)
                return bool(readable)
        except Exception:
            return False

    try:
        while True:
            try:
                # Read a line
                line = input()
                lines.append(line)

                # Check if there's more input immediately available (paste detection)
                if not is_paste_mode and len(lines) >= 1:
                    if _has_pending_input():
                        is_paste_mode = True

                # In paste mode, continue reading without displaying each line
                if is_paste_mode:
                    continue

            except EOFError:
                # Ctrl+D pressed - finish input
                break
    except KeyboardInterrupt:
        render_text("\nCancelled.", title="Cancelled")
        return None

    # Handle display based on mode and line count - show full content
    if is_paste_mode and len(lines) > 3:
        # For pasted content, show full view (no truncation)
        render_full("\n".join(lines), title="Input Content")

    print()  # New line after input

    # Join lines and return
    content = "\n".join(lines).strip()
    return content if content else None


@app.command(name="run")
def run_cmd(
    task: Optional[str] = typer.Argument(None, help="Task description"),
    resume: bool = typer.Option(False, "--resume", "-r", help="Resume interrupted flow"),
    loop: bool = typer.Option(False, "--loop", "-l", help="Loop mode (continuous task execution)"),
    max_iterations: int = typer.Option(10, "--max-iterations", "-n", help="Maximum iterations for loop mode"),
    type: str = typer.Option("feature", "--type", "-t", help="Task type (feature, bugfix, refactor, etc.)"),
    change: Optional[str] = typer.Option(None, "--change", "-c", help="Change name for this task"),
    flow_id: Optional[str] = typer.Option(None, "--flow-id", help="Specific flow ID to resume"),
    discover: bool = typer.Option(False, "--discover", "-d", help="Discovery mode - explore requirements with user before analyzing"),
):
    """SE3 Run — Unified entry point for the flow engine.

    Examples:
        se3 run "Implement user authentication"
        se3 run "Fix login bug" --type=bugfix
        se3 run --resume
        se3 run --loop
        se3 run --discover "I want to build something related to authentication"
    """
    from .commands.run import run_flow, run_loop_mode, get_project_root, handle_resume_interactive, SE3_DIR

    project_root = get_project_root()

    # Ensure se3 directory exists
    se3_dir = project_root / SE3_DIR
    se3_dir.mkdir(exist_ok=True)
    (se3_dir / "state").mkdir(exist_ok=True)
    (se3_dir / "cache").mkdir(exist_ok=True)

    # Handle discovery mode - force task_type to "discovery"
    if discover:
        type = "discovery"

    if loop:
        exit_code = run_loop_mode(
            project_root=project_root,
            initial_task=task,
            task_type=type,
            max_iterations=max_iterations,
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
                # No task provided, enter interactive multiline input mode
                task = _read_multiline_input()
                if not task:
                    render_full(
                        "Error: Task description required for new flow\n\n"
                        "Examples:\n"
                        '  se3 run "Implement feature X"\n'
                        "  se3 run --resume",
                        title="Error"
                    )
                    raise typer.Exit(1)

    # New flow mode
    if not task:
        # Enter interactive multiline input mode
        task = _read_multiline_input()
        if not task:
            render_full(
                "Error: Task description required (or use --resume)\n\n"
                "Examples:\n"
                '  se3 run "Implement feature X"\n'
                "  se3 run --resume",
                title="Error"
            )
            raise typer.Exit(1)

    exit_code = run_flow(
        project_root=project_root,
        task_description=task,
        task_type=type,
        change_name=change,
        is_loop_mode=False,
    )
    raise typer.Exit(exit_code)


# Register init command
from .commands import init_cmd
app.add_typer(init_cmd.app, name="init", help="Initialize SE3 project structure")


@app.command(name="guardrails")
def guardrails_cmd(
    spec_file: Path = typer.Argument(..., help="Path to spec file to check"),
    original: Optional[Path] = typer.Option(None, "--original", "-o", help="Path to original spec file for comparison"),
):
    """Check spec file against SE3 Spec Guardrails."""
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
            cwd=spec_file.parent,
            capture_output=True, text=True
        )
        if result.returncode == 0:
            original_content = result.stdout
        else:
            typer.echo(f"Warning: Could not find original version in git history")
            original_content = new_content  # Compare with itself (no violations)

    # Simple guardrails check
    violations = []

    # Check for weakened language patterns
    weaken_patterns = [
        (r'\bMUST\b', r'\b(SHOULD|MAY)\b', "MUST weakened to SHOULD/MAY"),
        (r'\bSHALL\b', r'\b(SHOULD|MAY)\b', "SHALL weakened to SHOULD/MAY"),
        (r'\bREQUIRED\b', r'\b(RECOMMENDED|OPTIONAL)\b', "REQUIRED weakened to RECOMMENDED/OPTIONAL"),
    ]

    for strong, weak, message in weaken_patterns:
        if re.search(strong, original_content) and re.search(weak, new_content):
            if not re.search(strong, new_content):
                violations.append({
                    "type": "WEAKENING",
                    "guardrail": "must_not_weaken",
                    "message": message
                })

    # Use full-content display for guardrails output
    lines = [
        "",
        "=" * 60,
        "SE 3.0 Spec Guardrails Check",
        "=" * 60,
        "",
        f"File: {spec_file}",
    ]

    if violations:
        lines.append(f"\n⚠️  {len(violations)} violation(s) found:")
        for v in violations:
            lines.append(f"\n  [{v['type']}] {v['message']}")
            lines.append(f"  Rule: {v['guardrail']}")
        lines.append(f"\n{'=' * 60}")
        render_full("\n".join(lines), title="Guardrails Check")
        raise typer.Exit(code=1)
    else:
        lines.append("\n✓ All guardrails passed - no violations found")
        lines.append(f"\n{'=' * 60}")
        render_full("\n".join(lines), title="Guardrails Check")
        raise typer.Exit(code=0)


if __name__ == "__main__":
    app()
