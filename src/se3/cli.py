"""SE 3.0 CLI - Main entry point for se3 commands."""

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.patch_stdout import patch_stdout

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


def _read_multiline_input(
    prompt_title: str = "Input",
    prompt_message: str = "Enter task description (Ctrl+D or Esc+Enter to finish, Ctrl+C to cancel):",
    history: Optional[any] = None,
    *,
    strip: bool = True,
) -> Optional[str]:
    """Read multiline input from stdin with proper Unicode support.

    Uses prompt_toolkit for interactive mode to correctly handle
    wide characters (e.g., Chinese) and multiline input.

    Args:
        prompt_title: Title displayed above the input area.
        prompt_message: Instruction text shown to the user.
        strip: Whether to strip leading/trailing whitespace from input.
               Default True. Set False when strict character comparison is
               needed (e.g., discovery confirmation gate's == "1" check).
    """
    # Check if stdin is a tty (interactive terminal)
    if not sys.stdin.isatty():
        # Non-interactive mode (pipe/redirect): read all at once, show full content
        try:
            content = sys.stdin.read()
            lines = content.split("\n")

            # Show full content for all input (no truncation)
            if lines:
                render_full("\n".join(lines), title=prompt_title)

            if strip:
                content = content.strip()
            return content if content else None
        except (EOFError, KeyboardInterrupt):
            return None

    # Interactive mode with prompt_toolkit for proper Unicode handling
    render_text(prompt_message, title=prompt_title)

    try:
        # Create custom key bindings to make Ctrl+D accept input
        # (mimicking traditional Unix behavior for multiline input)
        kb = KeyBindings()

        @kb.add(Keys.ControlD)
        def _(event):
            """Pressing Ctrl+D accepts the input (like traditional EOF).
            Always accepts — even with empty buffer (returns empty string).
            Ctrl+C is the only way to cancel (returns None).
            """
            buf = event.app.current_buffer
            buf.validate_and_handle()

        # Create a prompt session with multiline support, custom key bindings, and history
        session = PromptSession(
            multiline=True,
            message="> ",
            key_bindings=kb,
            history=history,
        )

        with patch_stdout():
            # Read input with prompt_toolkit
            content = session.prompt()

        # Show full content for review
        if strip:
            content = content.strip()
        if content:
            lines = content.split("\n")
            if len(lines) > 1:
                render_full(content, title=f"{prompt_title} Content")

        return content

    except KeyboardInterrupt:
        render_text("\nCancelled.", title="Cancelled")
        return None
    except EOFError:
        # Safety fallback — Ctrl+D should no longer raise EOFError,
        # but if it does, treat as empty input (not cancel)
        return ""


def _handle_list_loops(project_root) -> None:
    """Display existing unmerged loop branches."""
    from .engine.worktree import list_loop_branches

    branches = list_loop_branches(project_root)
    if not branches:
        render_text("No loop branches found.", title="Loop Branches")
        return

    lines = []
    for b in branches:
        commit_info = f"{b['commit_count']} commit(s) ahead of {b['base_branch']}"
        lines.append(f"  {b['branch']}  —  {commit_info}")

    render_full(
        "Existing loop branches:\n\n" + "\n".join(lines) + "\n\n"
        "To merge:  se3 run --loop --merge <branch>\n"
        "To discard: git branch -D <branch>",
        title="Loop Branches"
    )


@app.command(name="run")
def run_cmd(
    ctx: typer.Context,
    task: Optional[str] = typer.Argument(None, help="Task description"),
    resume: bool = typer.Option(False, "--resume", "-r", help="Resume interrupted flow"),
    loop: bool = typer.Option(False, "--loop", "-l", help="Loop mode (continuous task execution)"),
    max_iterations: int = typer.Option(10, "--max-iterations", "-n", help="Maximum iterations for loop mode"),
    type: str = typer.Option("feature", "--type", "-t", help="Task type (feature, bugfix, refactor, etc.)"),
    change: Optional[str] = typer.Option(None, "--change", "-c", help="Change name for this task"),
    flow_id: Optional[str] = typer.Option(None, "--flow-id", help="Specific flow ID to resume"),
    discover: bool = typer.Option(False, "--discover", "-d", help="Discovery mode - explore requirements with user before analyzing"),
    no_worktree: bool = typer.Option(False, "--no-worktree", help="Disable branch isolation in loop mode"),
    merge: Optional[str] = typer.Option(None, "--merge", help="Merge an existing loop branch (e.g. se3-loop/20260324-120000)"),
    list_loops: bool = typer.Option(False, "--list-loops", help="List existing unmerged loop branches"),
    from_issue: Optional[str] = typer.Option(None, "--from-issue", help="Run flow from an existing issue (ID or interactive selection)"),
    output_format: str = typer.Option("cli", "--output-format", help="Output sink: 'cli' (Rich rendering, default) or 'json' (structured NDJSON event stream)"),
    preset: Optional[str] = typer.Option(None, "--preset", help="Run a preset prompt task by name (mutually exclusive with --type; the preset carries its own type). Use '--preset list' to list available presets."),
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
    from .engine.prompt_history import get_prompt_history

    # Validate the output-format sink selection (the outermost sink choice).
    output_format = (output_format or "cli").lower()
    if output_format not in ("cli", "json"):
        render_full(
            f"Error: invalid --output-format '{output_format}'. "
            "Choose 'cli' or 'json'.",
            title="Error",
        )
        raise typer.Exit(1)

    project_root = get_project_root()

    # Create shared prompt history for this run session
    prompt_history = get_prompt_history(project_root)

    # Ensure se3 directory exists
    se3_dir = project_root / SE3_DIR
    se3_dir.mkdir(exist_ok=True)
    (se3_dir / "state").mkdir(exist_ok=True)
    (se3_dir / "cache").mkdir(exist_ok=True)

    # Handle preset prompts (common-task library). A preset carries its own
    # task type, so it is mutually exclusive with an explicit --type.
    if preset is not None:
        # Detect whether --type was explicitly passed on the command line.
        # Typer versions that vendor click expose ParameterSource via
        # typer._click; older versions delegate to the system click.
        try:
            from typer._click import core as _click_core

            _cmdline = _click_core.ParameterSource.COMMANDLINE
        except (ImportError, ModuleNotFoundError):
            from click.core import ParameterSource as _PS

            _cmdline = _PS.COMMANDLINE

        from .preset_loader import PresetError, list_presets, resolve

        if ctx.get_parameter_source("type") == _cmdline:
            raise typer.BadParameter(
                "--preset and --type are mutually exclusive; a preset carries "
                "its own task type.",
                param_hint="--preset",
            )

        if preset == "list":
            presets = list_presets(project_root)
            if not presets:
                render_full("No presets available.", title="Presets")
            else:
                lines = ["Available presets:", ""]
                for entry in presets:
                    lines.append(
                        f"  {entry.name}  (type={entry.type}, source={entry.layer})"
                    )
                render_full("\n".join(lines), title="Presets")
            raise typer.Exit(0)

        try:
            preset_type, preset_prompt, _layer = resolve(preset, project_root)
        except PresetError as exc:
            render_full(f"Error: {exc}", title="Error")
            raise typer.Exit(1)

        # A preset is equivalent to `se3 run --type <preset.type>
        # --description "<preset prompt full text>"`.
        type = preset_type
        task = preset_prompt

    # Handle discovery mode - force task_type to "discovery"
    if discover:
        type = "discovery"

    if list_loops:
        _handle_list_loops(project_root)
        raise typer.Exit(0)

    if loop or merge:
        exit_code = run_loop_mode(
            project_root=project_root,
            initial_task=task,
            task_type=type,
            max_iterations=max_iterations,
            prompt_history=prompt_history,
            no_worktree=no_worktree,
            merge_branch=merge,
            output_format=output_format,
        )
        raise typer.Exit(exit_code)

    # Handle --from-issue mode
    if from_issue is not None:
        from .engine.issue_manager import IssueManager, IssueStatus

        issue_mgr = IssueManager(project_root)
        issue_id = from_issue

        # If --from-issue given without value (empty string), interactive selection
        if not issue_id:
            open_issues = issue_mgr.list_issues(include_closed=False)
            if not open_issues:
                render_text("No open issues found.", title="Issues")
                raise typer.Exit(1)

            render_text("Open issues:", title="Select Issue")
            for iss in open_issues:
                prio = iss.priority if iss.priority else "-"
                typer.echo(f"  [{iss.id}] {iss.display_title} ({prio})")

            issue_id = typer.prompt("Enter issue ID")

        issue = issue_mgr.load(issue_id)
        if not issue:
            render_text(f"Issue '{issue_id}' not found.", title="Error")
            raise typer.Exit(1)

        if issue.status == IssueStatus.IN_PROGRESS:
            render_text(
                f"Issue '{issue_id}' is already in-progress. Use 'se3 issue reset {issue_id}' first.",
                title="Error",
            )
            raise typer.Exit(1)

        # Set issue to in-progress
        try:
            issue_mgr.update_status(issue.id, IssueStatus.IN_PROGRESS)
        except ValueError as e:
            render_text(f"Error: {e}", title="Error")
            raise typer.Exit(1)

        # Run flow with issue description
        exit_code = run_flow(
            project_root=project_root,
            task_description=issue.description,
            task_type=type,
            change_name=change,
            is_loop_mode=False,
            prompt_history=prompt_history,
            source_issue_id=issue.id,
            output_format=output_format,
        )

        # Update issue status based on result
        try:
            if exit_code == 0:
                issue_mgr.update_status(issue.id, IssueStatus.RESOLVED)
            else:
                issue_mgr.update_status(issue.id, IssueStatus.OPEN)
        except ValueError:
            pass  # Best effort

        raise typer.Exit(exit_code)

    if resume or flow_id:
        target_flow_id = flow_id

        if not target_flow_id:
            target_flow_id = handle_resume_interactive(project_root)

        if target_flow_id:
            exit_code = run_flow(
                project_root=project_root,
                flow_id=target_flow_id,
                prompt_history=prompt_history,
                output_format=output_format,
            )
            raise typer.Exit(exit_code)
        else:
            if not task:
                # No task provided, enter interactive multiline input mode
                task = _read_multiline_input(history=prompt_history)
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
        task = _read_multiline_input(history=prompt_history)
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
        prompt_history=prompt_history,
        output_format=output_format,
    )
    raise typer.Exit(exit_code)


# Import init command function (registered below)
from .commands.init_cmd import init_cmd as init_command

# Import history command
from .commands.history_cmd import app as history_app

# Import issue command
from .commands.issue_cmd import app as issue_app


@app.command(name="init")
def init_cmd(
    project_root: str = typer.Option(".", "--project-root", "-p", help="Project root directory"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Project name"),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing files"),
):
    """Initialize a new SE3 project.
    
    Creates the standard SE3 directory structure:
    - se3.yaml - Project configuration
    - se3/specs/ - Specification directory
    - se3/specs/base/spec.md - Base project specification
    """
    init_command(project_root=project_root, name=name, force=force)


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

    from .engine.merge.guardrails import check_spec_diff, GuardrailViolation

    # Run the shared guardrails check
    raw_violations = check_spec_diff(original_content, new_content, file_path=str(spec_file))
    violations = []
    for v in raw_violations:
        violations.append({
            "type": v.violation_type,
            "guardrail": "must_not_weaken" if v.violation_type == "WEAKENING" else "must_not_delete",
            "message": v.message,
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


# Register history command
app.add_typer(history_app, name="history", help="View and manage session history")

# Register issue command
app.add_typer(issue_app, name="issue", help="Manage SE3 issues")


# ---------------------------------------------------------------------------
# se3 daemon — the resident control-plane process
# ---------------------------------------------------------------------------
daemon_app = typer.Typer(
    name="daemon",
    help="Resident control-plane daemon (supervises local flows, dials the server)",
)


def _precheck_websockets(server_url: str) -> None:
    """Warn loudly on the CLI front-end when the WebSocket dep is missing.

    A daemon started with ``--server-url`` but without the ``websockets``
    package silently degrades to local-only mode (see
    ``DaemonClient.run``), so the machine never registers with the central
    server. The degradation is otherwise only logged to ``~/.se3/daemon.log``;
    this surfaces it where the user actually is.
    """
    try:
        import websockets  # type: ignore  # noqa: F401
    except Exception:
        render_text(
            "WARNING: the 'websockets' package is not installed.\n"
            "The daemon will start in LOCAL-ONLY mode and will NOT connect to\n"
            f"the central server ({server_url}); this machine will not appear\n"
            "in the web dashboard's machine list.\n"
            "Install it with: pip install 'se3[server]'",
            title="Daemon",
            # Render via Rich Text so the '[server]' extra is not eaten as markup.
            style="default",
        )


def _report_connection_result(config, daemon_status_fn) -> None:
    """Poll the daemon status file and report the real connection outcome.

    A detached daemon dials the server on its own event loop, so the CLI must
    read back the result via the status file. Polling is bounded so ``start``
    never hangs; if no verdict lands in time we say so rather than claim
    success.

    Two correctness requirements:

    * **Freshness** — a hard-killed previous daemon can leave a stale
      ``daemon_status.json`` behind. Status fields are only trusted once the
      file has been (re)written by the freshly-started daemon, i.e. once its
      ``updated_at`` is at or after the new daemon's ``started_at``.
    * **Transient errors are non-final** — the daemon's first dial may fail
      and record ``last_error`` before exponential-backoff reconnects ~1s
      later. We therefore keep polling on a recorded error and only report
      failure if the daemon is still not connected at the deadline.
    """
    deadline = time.time() + 8.0
    connected = False
    last_error = None
    while time.time() < deadline:
        status = daemon_status_fn(config)
        started_at = status.get("started_at")
        updated_at = status.get("updated_at")
        # Ignore a status file that predates the current daemon: it is a
        # leftover from a previous (possibly hard-killed) instance and its
        # connection fields say nothing about this start.
        is_fresh = (
            updated_at is not None
            and started_at is not None
            and updated_at >= started_at
        )
        if is_fresh:
            if status.get("connected"):
                connected = True
                break
            # A recorded error is treated as transient: remember it for the
            # final verdict but keep polling in case backoff reconnects.
            last_error = status.get("last_error") or last_error
        time.sleep(0.3)
    if connected:
        render_text("Connection: connected to the central server.", title="Daemon")
    elif last_error:
        render_text(
            f"WARNING: the daemon could not connect to the central server: "
            f"{last_error}.\n"
            "This machine will not appear in the web dashboard's machine list "
            "until it connects.",
            title="Daemon",
        )
    else:
        render_text(
            "WARNING: the daemon has not connected to the central server yet.\n"
            "Run 'se3 daemon status' to check the current connection state.",
            title="Daemon",
        )


@daemon_app.command(name="start")
def daemon_start_cmd(
    server_url: Optional[str] = typer.Option(
        None,
        "--server-url",
        help=(
            "Central server URL the daemon dials out to. An explicit port "
            "(wss://host:9000) is always preserved; when the port is omitted it "
            "is completed per the scheme — wss:// (and https://) default to 443, "
            "ws:// (and http://) default to 8080 (the se3-server plaintext "
            "default). So a bare wss://host dials :443, not :8080."
        ),
    ),
    daemon_key: Optional[str] = typer.Option(
        None,
        "--daemon-key",
        help=(
            "Secret daemon credential sent to the central server so it can "
            "bind this machine to its owner. When omitted, the SE3_DAEMON_KEY "
            "environment variable is used. The key is held only in memory and "
            "is never written to logs or the daemon status file. Prefer the "
            "environment variable so the secret does not land in shell history."
        ),
    ),
    foreground: bool = typer.Option(
        False, "--foreground", help="Run the daemon in the foreground (do not detach)"
    ),
):
    """Start the SE3 daemon.

    By default the daemon is launched as a detached background process. Pass
    ``--foreground`` to run it in the current terminal.
    """
    # Deferred import: the daemon package must not affect core CLI startup.
    from .daemon import DaemonConfig, DaemonAlreadyRunning, start_daemon, daemon_status

    # Pre-check the WebSocket dependency before launching so the user is told
    # up front when the daemon will silently fall back to local-only mode.
    if server_url:
        _precheck_websockets(server_url)

    # The explicit flag wins; otherwise fall back to the environment so a
    # secret need not appear on the command line / in shell history.
    resolved_key = daemon_key or os.environ.get("SE3_DAEMON_KEY") or None
    config = DaemonConfig(server_url=server_url, daemon_key=resolved_key)
    try:
        result = start_daemon(config, foreground=foreground)
    except DaemonAlreadyRunning as exc:
        render_text(str(exc), title="Daemon")
        raise typer.Exit(1)
    if not foreground:
        status = result.get("status")
        pid = result.get("pid")
        render_text(f"Daemon {status} (pid={pid})", title="Daemon")
        # The detached daemon dials the server on its own; read the real
        # connection result back from the status file.
        if server_url:
            _report_connection_result(config, daemon_status)
    raise typer.Exit(0)


@daemon_app.command(name="stop")
def daemon_stop_cmd():
    """Stop the running SE3 daemon."""
    from .daemon import DaemonConfig, stop_daemon

    result = stop_daemon(DaemonConfig())
    status = result.get("status")
    if status == "not_running":
        render_text("Daemon is not running.", title="Daemon")
        raise typer.Exit(0)
    if status == "stop_timeout":
        render_text(
            f"Daemon (pid={result.get('pid')}) did not stop within the timeout.",
            title="Daemon",
        )
        raise typer.Exit(1)
    render_text(f"Daemon stopped (pid={result.get('pid')}).", title="Daemon")
    raise typer.Exit(0)


@daemon_app.command(name="status")
def daemon_status_cmd(
    json_output: bool = typer.Option(
        False, "--json", "-j", help="Emit status as JSON"
    ),
):
    """Show the SE3 daemon's running state and tracked flows."""
    from .daemon import DaemonConfig, daemon_status

    status = daemon_status(DaemonConfig())
    if json_output:
        import json as _json

        typer.echo(_json.dumps(status, indent=2, ensure_ascii=False, default=str))
        raise typer.Exit(0)

    if not status.get("running"):
        render_text("Daemon is not running.", title="Daemon Status")
        raise typer.Exit(0)

    lines = [
        f"Running: yes (pid={status.get('pid')})",
        f"Machine: {status.get('machine_id')}",
        f"Server:  {status.get('server_url') or '(not configured)'}",
    ]
    # Real outbound-connection state, distinct from the configured-URL echo
    # above: a configured URL does not mean the daemon actually connected.
    if not status.get("server_url"):
        lines.append("Connection: local-only (no server configured)")
    elif status.get("connected"):
        lines.append("Connection: connected")
    else:
        # Surface the real failure reason. Earlier this fell back to the literal
        # "not connected" when last_error was empty, rendering the useless
        # "Connection: not connected (not connected)"; an empty reason now drops
        # the parenthetical entirely and points at the log instead of repeating
        # an information-free literal.
        reason = (status.get("last_error") or "").strip()
        if reason:
            lines.append(f"Connection: not connected ({reason})")
        else:
            lines.append(
                "Connection: not connected "
                "(reason unavailable — see daemon.log)"
            )
    tracked = status.get("tracked_flows") or []
    lines.append(f"Tracked flows: {len(tracked)}")
    for rec in tracked:
        flow_id = rec.get("flow_id") or "(unknown)"
        lines.append(f"  - pid={rec.get('pid')} flow={flow_id} ({rec.get('origin')})")
    render_text("\n".join(lines), title="Daemon Status")
    raise typer.Exit(0)


app.add_typer(daemon_app, name="daemon", help="Manage the SE3 daemon")


@app.command(name="sync")
def sync_cmd(
    once: bool = typer.Option(False, "--once", help="Run a single sync round (no convergence loop)"),
    max_rounds: int = typer.Option(10, "--max-rounds", help="Hard upper bound on rounds before aborting"),
    stable_rounds: int = typer.Option(1, "--stable-rounds", help="Consecutive zero-change rounds required to declare convergence"),
    interactive: bool = typer.Option(False, "--interactive", "-i", help="Pause for approval on high-impact requirement deletions"),
    show_diff: bool = typer.Option(False, "--show-diff", help="Print full per-round change list"),
    validate_only: bool = typer.Option(
        False,
        "--validate-only",
        help=(
            "Read-only audit mode: scan se3/specs/*/spec.md, run the "
            "spec-format v1 structural validator on each, print a "
            "results table, and exit 0 if every spec passes (1 "
            "otherwise). Does NOT call the LLM and does NOT write any "
            "files. Mutually exclusive with --once / --max-rounds / "
            "--stable-rounds / --interactive / --show-diff (other flags "
            "are ignored in this mode)."
        ),
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        help=(
            "Resume a previous sync run from the checkpoint at "
            "se3/state/sync_checkpoint.json. Already-in-sync specs whose "
            "content sha256 has not changed are skipped; changed and "
            "previously-failed specs are re-analyzed. Mutually exclusive "
            "with --validate-only."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Ignore sync_state.json cache and force a full analysis of "
            "all specs from scratch. After convergence, the cache is "
            "rewritten with fresh results."
        ),
    ),
    confirm_cleanup: bool = typer.Option(
        False,
        "--confirm-cleanup",
        help=(
            "When obsolete specs are detected (code entirely removed), "
            "prompt for confirmation before deleting each one. Without "
            "this flag, obsolete specs are deleted automatically."
        ),
    ),
):
    """Run code → spec sync until convergence.

    Spec is the documented snapshot of code (spec-assistant); this command
    rewrites specs to match the current code in repeated rounds until no
    further drift is found (convergence) or ``--max-rounds`` is reached.

    Examples:
        se3 sync                              # loop until converged
        se3 sync --once                       # one round only
        se3 sync --max-rounds 5 --stable-rounds 2
        se3 sync --interactive                # approve high-impact deletions
        se3 sync --show-diff                  # also dump per-round changes
        se3 sync --validate-only              # audit specs without running sync
    """
    if validate_only and resume:
        render_text(
            "--validate-only and --resume are mutually exclusive.",
            title="Error",
        )
        raise typer.Exit(1)

    if validate_only:
        if once or interactive or show_diff or force or confirm_cleanup:
            render_text(
                "--validate-only ignores --once / --interactive / "
                "--show-diff / --force / --confirm-cleanup.",
                title="SE3 Sync",
            )
        from .commands.sync import validate_only_command
        code = validate_only_command()
        raise typer.Exit(code)

    from .commands.sync import sync_command

    if once:
        # --once collapses to a single-round invocation. We deliberately ignore
        # user-supplied max_rounds / stable_rounds in this case so the semantics
        # stay obvious: "exactly one round, no convergence check."
        max_rounds = 1
        stable_rounds = 1

    if max_rounds < 1:
        render_text("--max-rounds must be >= 1.", title="Error")
        raise typer.Exit(1)
    if stable_rounds < 1:
        render_text("--stable-rounds must be >= 1.", title="Error")
        raise typer.Exit(1)
    if stable_rounds > max_rounds:
        render_text(
            f"--stable-rounds ({stable_rounds}) cannot exceed --max-rounds ({max_rounds}).",
            title="Error",
        )
        raise typer.Exit(1)

    sync_command(
        max_rounds=max_rounds,
        stable_rounds=stable_rounds,
        interactive=interactive,
        show_diff=show_diff,
        once=once,
        resume=resume,
        force=force,
        confirm_cleanup=confirm_cleanup,
    )


@app.command(name="sync-respond")
def sync_respond_cmd(
    call_file: Path = typer.Argument(..., help="Path to the sync call file"),
):
    """Process an MCP call response file for sync high-impact deletions.

    The single-directional sync flow only emits one kind of call file:
    ``sync_high_impact_deletion``. After editing the ``.response`` file
    (filling each item's ``decision`` with ``approve`` or ``skip``), run
    this command to apply the approved deletions.

    Example:
        se3 sync-respond se3/calls/sync_high_impact_deletion_12345.json
    """
    from .commands.sync import process_call_response

    process_call_response(call_file)


@app.command(name="merge")
def merge_cmd(
    branches: list[str] = typer.Argument(None, help="Branches to merge into current branch (in order)"),
    strategy: str = typer.Option(None, "--strategy", "-s", help="Conflict resolution strategy: fast (default), safe, or strict"),
    delete_merged: bool = typer.Option(None, "--delete-merged", "-d", help="Delete merged branches and archive their worktrees (default: enabled; use --no-delete-merged to disable)"),
    no_delete_merged: bool = typer.Option(False, "--no-delete-merged", help="Do not delete merged branches (overrides config and the new default-on behaviour)"),
):
    """Merge one or more branches sequentially into the current branch.

    Each branch is merged in order using git merge. Conflicts are handled
    per the chosen strategy. After all merges complete, SemVer bumps are
    aggregated and applied to pyproject.toml.

    Default strategy and delete-merged behaviour can be configured in
    se3.yaml under the ``merge:`` section.

    Examples:
        se3 merge feature-x
        se3 merge feature-x feature-y --strategy=strict
        se3 merge feature-x --delete-merged
    """
    from .commands.merge_cmd import run_merge, validate_branch_names
    from .commands.run import get_project_root
    from .config import load_merge_config

    # Defect I1: empty branch list must be rejected at CLI input level so a
    # silent zero-iteration "success" is impossible. typer.BadParameter renders
    # the standard click-style error including --help hint.
    if not branches:
        raise typer.BadParameter(
            "At least one branch name is required.\n"
            "Usage: se3 merge <branch> [<branch> ...]",
            param_hint="branches",
        )

    # Defect I2: branch names with leading-dash or shell metachars must be
    # rejected before any git command is constructed. Otherwise a name like
    # ``-rf`` could be interpreted by git/shell as a flag, and metachars in
    # logged command lines could mislead operators.
    try:
        validate_branch_names(branches)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="branches") from exc

    # Resolve project root first so config is loaded from the correct location
    # (not cwd, which may be a subdirectory of the project).
    project_root = get_project_root()
    merge_cfg = load_merge_config(project_root)
    effective_strategy = strategy if strategy is not None else merge_cfg.strategy
    if no_delete_merged:
        effective_delete = False
    else:
        effective_delete = delete_merged if delete_merged is not None else merge_cfg.delete_merged_default

    from .engine.merge.conflict_resolver import MergeStrategy

    try:
        effective_strategy = MergeStrategy.from_str(effective_strategy).value
    except ValueError as exc:
        # ``MergeStrategy.from_str`` already carries a migration-friendly
        # message for ``default`` / ``robust``; surface it through
        # ``BadParameter`` so the user sees the same Click-style error
        # rendering as other CLI argument failures.
        raise typer.BadParameter(str(exc), param_hint="--strategy") from exc

    exit_code = run_merge(
        branches=branches,
        strategy=effective_strategy,
        delete_merged=effective_delete,
        strict_runtime_sync=merge_cfg.strict_runtime_sync,
    )
    raise typer.Exit(exit_code)


@app.command(name="merge-respond")
def merge_respond_cmd(
    call_file: Path = typer.Argument(..., help="Path to the merge call file"),
):
    """Process an MCP call response file for merge conflicts.

    After editing the .response file for a merge call, run this command
    to execute the conflict decisions (accept LLM resolution, abort, or manual).

    Example:
        se3 merge-respond se3/calls/merge_20240101_120000_feat-x.json
    """
    from .commands.merge_respond import process_merge_response

    exit_code = process_merge_response(call_file)
    raise typer.Exit(exit_code)


@app.command(name="salvage")
def salvage_cmd(
    project_root: Optional[str] = typer.Option(None, "--project-root", "-p", help="Project root directory"),
):
    """Salvage work from an abnormally terminated session.

    Performs best-effort recovery:
    1. Reads session state (tolerant of corruption)
    2. Evaluates git diff
    3. Commits existing changes
    4. Creates issues for unfinished work
    5. Archives the session

    Use this when a session crashed or was interrupted.
    After salvage, use 'se3 run --from-issue' to continue work.
    """
    from .commands.salvage_cmd import salvage

    root = Path(project_root) if project_root else None
    exit_code = salvage(root)
    raise typer.Exit(exit_code)


if __name__ == "__main__":
    app()
