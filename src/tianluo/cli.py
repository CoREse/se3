"""SE 3.0 CLI - Main entry point for luo commands."""

from tianluo.runtime_paths import runtime_dir
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
from .i18n import bind_project_root, t


app = typer.Typer(
    name="luo",
    help=t("cli.help.app"),
    invoke_without_command=True,
)


def legacy_app() -> None:
    """Entry point for the deprecated ``se3`` console script (12.x alias).

    Prints a one-line migration notice on stderr, then runs the normal CLI.
    The alias — and this wrapper — are removed in 13.0.0.
    """
    print(
        "se3: this command was renamed in 12.0.0 — use `luo` "
        "(full name: `tianluo`). The `se3` alias will be removed in 13.0.0.",
        file=sys.stderr,
    )
    app()

def _version_callback(value: bool):
    """Handle --version flag."""
    if value:
        typer.echo(t("cli.version", version=__version__))
        raise typer.Exit()

@app.callback(help=t("cli.help.app"))
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", "-v", help=t("cli.help.version"), callback=_version_callback, is_eager=True
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
    prompt_title: Optional[str] = None,
    prompt_message: Optional[str] = None,
    history: Optional[any] = None,
    *,
    strip: bool = True,
) -> Optional[str]:
    """Read multiline input from stdin with proper Unicode support.

    Uses prompt_toolkit for interactive mode to correctly handle
    wide characters (e.g., Chinese) and multiline input.

    Args:
        prompt_title: Title displayed above the input area. ``None`` renders the
            default title at call time.
        prompt_message: Instruction text shown to the user. ``None`` renders the
            default message at call time.
        strip: Whether to strip leading/trailing whitespace from input.
               Default True. Set False when strict character comparison is
               needed (e.g., discovery confirmation gate's == "1" check).
    """
    # WHY: the default chrome is resolved here, not in the signature — signature
    # defaults evaluate at import time, before the command binds the project root
    # and its ``language.language`` takes effect, which would pin the prompt to
    # the cwd-resolved language while the rest of the output follows the project.
    if prompt_title is None:
        prompt_title = t("cli.input.title")
    if prompt_message is None:
        prompt_message = t("cli.input.prompt")

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
                render_full(content, title=t("cli.input.content_title", title=prompt_title))

        return content

    except KeyboardInterrupt:
        render_text(t("cli.input.cancelled_message"), title=t("cli.common.cancelled"))
        return None
    except EOFError:
        # Safety fallback — Ctrl+D should no longer raise EOFError,
        # but if it does, treat as empty input (not cancel)
        return ""


# Task types a user may name explicitly via ``--type``. Deliberately excludes
# "discovery": that is a run *mode* reached only through ``--discover``, never a
# classification the user asks analyze to honour.
EXPLICIT_TASK_TYPES = ("feature", "bugfix", "review", "small", "survey")


def _param_from_commandline(ctx: typer.Context, name: str) -> bool:
    """Report whether *name* was actually typed on the command line.

    WHY: a Typer option with a non-None default is indistinguishable from an
    explicitly-passed value by looking at the value alone, and ``--type``'s
    downstream meaning depends on that distinction (an explicit type is
    persisted as ``explicit_type`` and overrides analyze's classification).
    ParameterSource is the only reliable signal. Typer versions that vendor
    click expose it via ``typer._click``; older ones delegate to system click.
    """
    try:
        from typer._click import core as _click_core

        cmdline = _click_core.ParameterSource.COMMANDLINE
    except (ImportError, ModuleNotFoundError):
        from click.core import ParameterSource as _PS

        cmdline = _PS.COMMANDLINE

    return ctx.get_parameter_source(name) == cmdline


@app.command(name="run", help=t("cli.help.run.desc"))
def run_cmd(
    ctx: typer.Context,
    task: Optional[str] = typer.Argument(None, help=t("cli.help.run.task")),
    resume: bool = typer.Option(False, "--resume", "-r", help=t("cli.help.run.resume")),
    # WHY: the default is the "pending" sentinel, not a real type. A concrete
    # default made every run look like an explicit --type to run_flow, which
    # then wrote explicit_type into the flow context and let it override
    # analyze's classification — so an unflagged run was silently pinned to
    # "feature" and the classifier's answer was discarded.
    type: str = typer.Option("pending", "--type", "-t", help=t("cli.help.run.type")),
    change: Optional[str] = typer.Option(None, "--change", "-c", help=t("cli.help.run.change")),
    flow_id: Optional[str] = typer.Option(None, "--flow-id", help=t("cli.help.run.flow_id")),
    discover: bool = typer.Option(False, "--discover", "-d", help=t("cli.help.run.discover")),
    from_issue: Optional[str] = typer.Option(None, "--from-issue", help=t("cli.help.run.from_issue")),
    output_format: str = typer.Option("cli", "--output-format", help=t("cli.help.run.output_format")),
    preset: Optional[str] = typer.Option(None, "--preset", help=t("cli.help.run.preset")),
    worktree: bool = typer.Option(False, "--worktree", help=t("cli.help.run.worktree")),
):
    """SE3 Run — Unified entry point for the flow engine.

    Examples:
        luo run "Implement user authentication"
        luo run "Fix login bug" --type=bugfix
        luo run --resume
        luo run --discover "I want to build something related to authentication"
        luo run --worktree "Implement feature X"   # isolated worktree, auto-merged back
    """
    from .commands.run import (
        run_flow,
        run_worktree_mode,
        resume_run,
        get_project_root,
        handle_resume_interactive,
        from_issue_flow_state_exists,
        snapshot_from_issue_flow_ids,
        SE3_DIR,
    )
    from .engine.prompt_history import get_prompt_history

    # Validate the output-format sink selection (the outermost sink choice).
    output_format = (output_format or "cli").lower()
    if output_format not in ("cli", "json"):
        render_full(
            t("cli.run.invalid_output_format", output_format=output_format),
            title=t("cli.common.error"),
        )
        raise typer.Exit(1)

    # Validate an explicitly-typed --type before anything else touches it.
    # Only a command-line-sourced value is checked: --preset supplies its own
    # type from the preset file and --discover overwrites it with the
    # "discovery" run mode, and neither is a user-authored classification.
    # WHY reject here rather than in get_default_step_sequence: that lookup must
    # keep silently falling back for retired types still persisted in old flows.
    # WHY "pending" is not explicit even when typed: it is the auto-detect
    # sentinel meaning "let analyze classify". The daemon spawner always appends
    # `--type <task_type>`, and the WebUI's default "auto" option posts exactly
    # "pending" — so an explicitly-passed sentinel must behave like no --type at
    # all, both for this validation and for the --preset exclusivity check.
    type_is_explicit = _param_from_commandline(ctx, "type") and type != "pending"
    if type_is_explicit and type not in EXPLICIT_TASK_TYPES:
        render_full(
            t(
                "cli.run.invalid_task_type",
                task_type=type,
                valid_types=", ".join(EXPLICIT_TASK_TYPES),
            ),
            title=t("cli.common.error"),
        )
        raise typer.Exit(1)

    project_root = get_project_root()

    # Create shared prompt history for this run session
    prompt_history = get_prompt_history(project_root)

    # Ensure luo directory exists
    se3_dir = runtime_dir(project_root)
    se3_dir.mkdir(exist_ok=True)
    (se3_dir / "state").mkdir(exist_ok=True)
    (se3_dir / "cache").mkdir(exist_ok=True)

    # Handle preset prompts (common-task library). A preset carries its own
    # task type, so it is mutually exclusive with an explicit --type.
    if preset is not None:
        from .preset_loader import PresetError, list_presets, resolve

        if type_is_explicit:
            raise typer.BadParameter(
                t("cli.preset.mutually_exclusive"),
                param_hint="--preset",
            )

        if preset == "list":
            presets = list_presets(project_root)
            if not presets:
                render_full(t("cli.preset.none_available"), title=t("cli.preset.title"))
            else:
                lines = [t("cli.preset.available_header"), ""]
                for entry in presets:
                    lines.append(
                        t("cli.preset.entry", name=entry.name, type=entry.type, layer=entry.layer)
                    )
                render_full("\n".join(lines), title=t("cli.preset.title"))
            raise typer.Exit(0)

        try:
            preset_type, preset_prompt, _layer = resolve(preset, project_root)
        except PresetError as exc:
            render_full(t("cli.preset.resolve_error", error=exc), title=t("cli.common.error"))
            raise typer.Exit(1)

        # A preset is equivalent to `luo run --type <preset.type>
        # --description "<preset prompt full text>"`.
        type = preset_type
        task = preset_prompt

    # Handle discovery mode - force task_type to "discovery"
    if discover:
        type = "discovery"

    # Handle --from-issue mode
    if from_issue is not None:
        from .engine.issue_manager import IssueManager, IssueStatus

        issue_mgr = IssueManager(project_root)
        issue_id = from_issue

        # If --from-issue given without value (empty string), interactive selection
        if not issue_id:
            open_issues = issue_mgr.list_issues(include_closed=False)
            if not open_issues:
                render_text(t("cli.issue.none_open"), title=t("cli.issue.title"))
                raise typer.Exit(1)

            render_text(t("cli.issue.open_header"), title=t("cli.issue.select_title"))
            for iss in open_issues:
                prio = iss.priority if iss.priority else "-"
                typer.echo(t("cli.issue.entry", id=iss.id, title=iss.display_title, priority=prio))

            issue_id = typer.prompt(t("cli.issue.enter_id"))

        issue = issue_mgr.load(issue_id)
        if not issue:
            render_text(t("cli.issue.not_found", issue_id=issue_id), title=t("cli.common.error"))
            raise typer.Exit(1)

        if issue.status == IssueStatus.IN_PROGRESS:
            render_text(
                t("cli.issue.already_in_progress", issue_id=issue_id),
                title=t("cli.common.error"),
            )
            raise typer.Exit(1)

        # Set issue to in-progress
        try:
            issue_mgr.update_status(issue.id, IssueStatus.IN_PROGRESS)
        except ValueError as e:
            render_text(t("cli.issue.status_error", error=e), title=t("cli.common.error"))
            raise typer.Exit(1)

        # Snapshot the flow_ids that ALREADY carry this source issue BEFORE
        # dispatching, so the post-dispatch self-recovery below can tell state
        # THIS dispatch persisted from a stale prior run's leftover engine.json.
        # The main-repo engine.json is a single reused slot: after an earlier
        # `--from-issue <id>` run of the SAME (now resolved/open) issue completed,
        # it still carries that source_issue_id until the next run overwrites it —
        # so an id-only existence check would see the stale slot and wrongly skip
        # the revert when the current dispatch fails before persisting anything.
        prior_flow_ids = snapshot_from_issue_flow_ids(
            project_root, bool(worktree), issue.id
        )

        # Run flow with issue description. Honour --worktree: an isolated
        # from-issue run goes through run_worktree_mode (which threads
        # source_issue_id through and merges back on success) so the
        # daemon-spawned `--from-issue <id> --worktree` path is not silently
        # downgraded to a synchronous in-place run.
        if worktree:
            exit_code = run_worktree_mode(
                project_root=project_root,
                task=issue.description,
                task_type=type,
                change_name=change,
                prompt_history=prompt_history,
                source_issue_id=issue.id,
                output_format=output_format,
            )
        else:
            exit_code = run_flow(
                project_root=project_root,
                task_description=issue.description,
                task_type=type,
                change_name=change,
                prompt_history=prompt_history,
                source_issue_id=issue.id,
                output_format=output_format,
            )

        # Issue finalization is NOT done here on exit_code: it is an unreliable
        # signal — in json output mode a pause also returns 0, which used to
        # resolve the issue on the very first pause. It also never fired on a
        # daemon/`--resume` continuation, which re-enters via run_flow in a new
        # process without this wrapper. Terminal finalization is instead owned by
        # the flow's true terminal state: run_flow._finalize_sync_source_issue
        # for synchronous runs, and the trailing luo merge for --worktree runs
        # (only a successful merge-back resolves). Both key off the persisted
        # flow.source_issue_id, so they are process-independent.
        #
        # ONE gap the terminal hooks cannot cover: a dispatch that fails BEFORE
        # any flow state is persisted (fork_worktree/get_current_branch raising,
        # or a pre-flow ConfigError). No engine.json ever carries this
        # source_issue_id, so nothing downstream can finalize it and the issue
        # would stay IN_PROGRESS forever — re-running --from-issue is then blocked
        # by the in-progress gate. Revert the OPEN→IN_PROGRESS transition we just
        # made, but ONLY when no persisted flow carries the issue: a paused flow
        # (or a COMPLETED worktree flow whose merge failed, held IN_PROGRESS for a
        # retry-merge) owns its own finalize and must not be clobbered here.
        if exit_code != 0 and not from_issue_flow_state_exists(
            project_root, bool(worktree), issue.id, prior_flow_ids
        ):
            try:
                current = issue_mgr.load(issue.id)
                if current is not None and current.status == IssueStatus.IN_PROGRESS:
                    issue_mgr.update_status(issue.id, IssueStatus.OPEN)
            except Exception:
                # Best-effort self-recovery — never mask the dispatch's exit code.
                pass
        raise typer.Exit(exit_code)

    if resume or flow_id:
        target_flow_id = flow_id

        if not target_flow_id:
            target_flow_id = handle_resume_interactive(project_root)

        if target_flow_id:
            # resume_run resolves whether the selected flow is an isolated
            # --worktree run (re-dispatch inside its worktree + trailing merge)
            # or a plain main-repo flow (resumed in place under the main lock).
            exit_code = resume_run(
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
                        t("cli.run.task_required_new_flow"),
                        title=t("cli.common.error")
                    )
                    raise typer.Exit(1)

    # New flow mode
    if not task:
        # Enter interactive multiline input mode
        task = _read_multiline_input(history=prompt_history)
        if not task:
            render_full(
                t("cli.run.task_required"),
                title=t("cli.common.error")
            )
            raise typer.Exit(1)

    if worktree:
        # Isolation mode: run the same flow in a dedicated worktree, then
        # auto-merge the result back into the current branch.
        exit_code = run_worktree_mode(
            project_root=project_root,
            task=task,
            task_type=type,
            change_name=change,
            prompt_history=prompt_history,
            output_format=output_format,
        )
    else:
        exit_code = run_flow(
            project_root=project_root,
            task_description=task,
            task_type=type,
            change_name=change,
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

# Import code-index command (structure-map navigation; takes over the per-module
# locator-navigation role the old base spec carried)
from .commands.code_index_cmd import app as code_index_app

# Import migrate command (registry-based version/format migration channel)
from .commands.migrate_cmd import migrate_app

# Import worktree command (isolation-worktree operator surface: `gc` reclaims
# leaked terminal --worktree runs). Imports only the engine GC core, no server
# deps, so core/server dependency isolation is preserved.
from .commands.worktree_cmd import worktree_app

# Import e2e command (manual trigger for the end-to-end subsystem). Its module
# level imports nothing from tianluo.e2e — every one of them sits inside a
# command body — so building the command tree on a core-only install never
# reaches for the `tianluo[e2e]` extra.
from .commands.e2e_cmd import e2e_app


@app.command(name="init", help=t("cli.help.init.desc"))
def init_cmd(
    project_root: str = typer.Option(".", "--project-root", "-p", help=t("cli.help.common.project_root")),
    name: Optional[str] = typer.Option(None, "--name", "-n", help=t("cli.help.init.name")),
    force: bool = typer.Option(False, "--force", "-f", help=t("cli.help.init.force")),
):
    """Initialize a new SE3 project.
    
    Creates the standard SE3 directory structure:
    - tianluo.yaml - Project configuration
    - tianluo/specs/ - Specification directory
    - tianluo/specs/base/spec.md - Base project specification
    """
    bind_project_root(Path(project_root))
    init_command(project_root=project_root, name=name, force=force)


def _run_spec_size_guardrails(project_root: Optional[Path]) -> None:
    """Run the spec volume-governance size checks and emit the report.

    Reads ``SpecGovernanceConfig.guardrails_size_tier`` to decide behaviour:
    ``warn`` (default) prints any violations and exits ``0`` (non-blocking),
    while ``enforce`` prints them and exits ``1`` (intercept). When no
    violations are found the command always exits ``0``.
    """
    root = Path(project_root).resolve() if project_root else Path.cwd()
    bind_project_root(root)

    from .config import load_spec_governance_config
    from .engine.merge.guardrails import check_spec_sizes

    config = load_spec_governance_config(root)
    violations = check_spec_sizes(root, config)
    tier = config.guardrails_size_tier

    lines = [
        "",
        "=" * 60,
        t("cli.guardrails.size_check_header"),
        "=" * 60,
        "",
        t("cli.guardrails.project", root=root),
        t("cli.guardrails.tier", tier=tier),
    ]

    if violations:
        lines.append(t("cli.guardrails.size_violations_found", count=len(violations)))
        for v in violations:
            ev = v.evidence or {}
            size_b = ev.get("size_bytes")
            limit_b = ev.get("limit_bytes")
            detail = t("cli.guardrails.size_detail", size=size_b, limit=limit_b) if size_b is not None and limit_b is not None else ""
            lines.append(f"\n  [{v.violation_type}] {v.file_path}{detail}")
            lines.append(f"  {v.message}")
        lines.append(f"\n{'=' * 60}")
        if tier == "enforce":
            lines.append(t("cli.guardrails.tier_enforce_failing"))
            render_full("\n".join(lines), title=t("cli.guardrails.size_title"))
            raise typer.Exit(code=1)
        lines.append(t("cli.guardrails.tier_warn_reporting"))
        render_full("\n".join(lines), title=t("cli.guardrails.size_title"))
        raise typer.Exit(code=0)

    lines.append(t("cli.guardrails.size_all_passed"))
    lines.append(f"\n{'=' * 60}")
    render_full("\n".join(lines), title=t("cli.guardrails.size_title"))
    raise typer.Exit(code=0)


@app.command(name="guardrails", help=t("cli.help.guardrails.desc"))
def guardrails_cmd(
    spec_file: Optional[Path] = typer.Argument(None, help=t("cli.help.guardrails.spec_file")),
    original: Optional[Path] = typer.Option(None, "--original", "-o", help=t("cli.help.guardrails.original")),
    sizes: bool = typer.Option(False, "--sizes", help=t("cli.help.guardrails.sizes")),
    project_root: Optional[Path] = typer.Option(None, "--project-root", "-p", help=t("cli.help.guardrails.project_root")),
):
    """Check spec file against SE3 Spec Guardrails."""
    # Both branches render through t(); bind here so --project-root selects the
    # target project's language for the spec-file check too, not just --sizes.
    bind_project_root(Path(project_root) if project_root else None)

    if sizes:
        _run_spec_size_guardrails(project_root)
        return

    if spec_file is None:
        typer.echo(t("cli.guardrails.spec_file_required"), err=True)
        raise typer.Exit(code=1)

    if not spec_file.exists():
        typer.echo(t("cli.guardrails.spec_file_not_found", spec_file=spec_file), err=True)
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
            typer.echo(t("cli.guardrails.no_original_in_git"))
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

    # Size governance runs automatically alongside the per-file diff check (no
    # separate --sizes flag needed). The configured tier decides whether size
    # violations block: ``enforce`` fails the command, ``warn`` (default) only
    # reports. Resolve the project root from the spec file's location.
    size_root = _project_root_for_spec(spec_file)
    from .config import load_spec_governance_config
    from .engine.merge.guardrails import check_spec_sizes

    gov_config = load_spec_governance_config(size_root)
    size_violations = check_spec_sizes(size_root, gov_config)
    size_tier = gov_config.guardrails_size_tier
    size_blocks = bool(size_violations) and size_tier == "enforce"

    # Use full-content display for guardrails output
    lines = [
        "",
        "=" * 60,
        t("cli.guardrails.check_header"),
        "=" * 60,
        "",
        t("cli.guardrails.file", spec_file=spec_file),
    ]

    if violations:
        lines.append(t("cli.guardrails.violations_found", count=len(violations)))
        for v in violations:
            lines.append(f"\n  [{v['type']}] {v['message']}")
            lines.append(t("cli.guardrails.rule", guardrail=v['guardrail']))
    else:
        lines.append(t("cli.guardrails.content_passed"))

    if size_violations:
        lines.append(
            t("cli.guardrails.size_violations_found_tier", count=len(size_violations), tier=size_tier)
        )
        for v in size_violations:
            ev = v.evidence or {}
            size_b = ev.get("size_bytes")
            limit_b = ev.get("limit_bytes")
            detail = (
                t("cli.guardrails.size_detail", size=size_b, limit=limit_b)
                if size_b is not None and limit_b is not None
                else ""
            )
            lines.append(f"\n  [{v.violation_type}] {v.file_path}{detail}")
            lines.append(f"  {v.message}")
        if size_tier == "enforce":
            lines.append(t("cli.guardrails.size_tier_enforce_failing"))
        else:
            lines.append(t("cli.guardrails.size_tier_warn_reporting"))

    lines.append(f"\n{'=' * 60}")
    render_full("\n".join(lines), title=t("cli.guardrails.check_title"))
    if violations or size_blocks:
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


def _project_root_for_spec(spec_file: Path) -> Path:
    """Resolve the SE3 project root that owns *spec_file* for size governance."""
    try:
        p = spec_file.resolve()
    except OSError:
        p = spec_file
    for anc in [p] + list(p.parents):
        if (runtime_dir(anc) / "specs").is_dir() \
                or (anc / "tianluo.yaml").is_file() \
                or (anc / "tianluo.local.yaml").is_file():
            return anc
    return Path.cwd()


# Register history command
app.add_typer(history_app, name="history", help=t("cli.help.history"))

# Register issue command
app.add_typer(issue_app, name="issue", help=t("cli.help.issue"))

# Register code-index command (structure map navigation: index / show / rebuild
# / inspect). Named "code-index" — not the ambiguous "code" — and reads the
# authoritative tianluo/code-index.md product.
app.add_typer(
    code_index_app,
    name="code-index",
    help=t("cli.help.code_index"),
)

# Register migrate command (registry-based version/format migration channel)
app.add_typer(migrate_app, name="migrate", help=t("cli.help.migrate"))

# Register worktree command (isolation-worktree operator surface; `luo worktree
# gc` reclaims leaked terminal --worktree runs stranded under tianluo/worktrees/)
app.add_typer(worktree_app, name="worktree", help=t("cli.help.worktree"))

# Register e2e command (run / list / doctor / bootstrap). Shares session.run_e2e
# with the engine's E2E step so a manual run and a flow run cannot diverge.
app.add_typer(e2e_app, name="e2e", help=t("cli.help.e2e"))


# ---------------------------------------------------------------------------
# luo daemon — the resident control-plane process
# ---------------------------------------------------------------------------
daemon_app = typer.Typer(
    name="daemon",
    help=t("cli.help.daemon.app"),
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
            t("cli.daemon.websockets_missing", server_url=server_url),
            title=t("cli.daemon.title"),
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
        render_text(t("cli.daemon.connected"), title=t("cli.daemon.title"))
    elif last_error:
        render_text(
            t("cli.daemon.connect_failed", last_error=last_error),
            title=t("cli.daemon.title"),
        )
    else:
        render_text(
            t("cli.daemon.not_connected_yet"),
            title=t("cli.daemon.title"),
        )


@daemon_app.command(name="start", help=t("cli.help.daemon.start.desc"))
def daemon_start_cmd(
    server_url: Optional[str] = typer.Option(
        None,
        "--server-url",
        help=t("cli.help.daemon.start.server_url"),
    ),
    daemon_key: Optional[str] = typer.Option(
        None,
        "--daemon-key",
        help=t("cli.help.daemon.start.daemon_key"),
    ),
    foreground: bool = typer.Option(
        False, "--foreground", help=t("cli.help.daemon.start.foreground")
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
        # The exception body is an English f-string built in daemon.py; render
        # the localized catalog entry from the pid it carries instead.
        render_text(
            t("cli.daemon.already_running", pid=exc.pid),
            title=t("cli.daemon.title"),
        )
        raise typer.Exit(1)
    if not foreground:
        status = result.get("status")
        pid = result.get("pid")
        render_text(t("cli.daemon.started", status=status, pid=pid), title=t("cli.daemon.title"))
        # The detached daemon dials the server on its own; read the real
        # connection result back from the status file.
        if server_url:
            _report_connection_result(config, daemon_status)
    raise typer.Exit(0)


@daemon_app.command(name="stop", help=t("cli.help.daemon.stop.desc"))
def daemon_stop_cmd():
    """Stop the running SE3 daemon."""
    from .daemon import DaemonConfig, stop_daemon

    result = stop_daemon(DaemonConfig())
    status = result.get("status")
    if status == "not_running":
        render_text(t("cli.daemon.not_running"), title=t("cli.daemon.title"))
        raise typer.Exit(0)
    if status == "stop_timeout":
        render_text(
            t("cli.daemon.stop_timeout", pid=result.get('pid')),
            title=t("cli.daemon.title"),
        )
        raise typer.Exit(1)
    render_text(t("cli.daemon.stopped", pid=result.get('pid')), title=t("cli.daemon.title"))
    raise typer.Exit(0)


@daemon_app.command(name="status", help=t("cli.help.daemon.status.desc"))
def daemon_status_cmd(
    json_output: bool = typer.Option(
        False, "--json", "-j", help=t("cli.help.daemon.status.json")
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
        render_text(t("cli.daemon.not_running"), title=t("cli.daemon.status_title"))
        raise typer.Exit(0)

    lines = [
        t("cli.daemon.status_running", pid=status.get('pid')),
        t("cli.daemon.status_machine", machine_id=status.get('machine_id')),
        t("cli.daemon.status_server", server=status.get('server_url') or t("cli.daemon.not_configured")),
    ]
    # Real outbound-connection state, distinct from the configured-URL echo
    # above: a configured URL does not mean the daemon actually connected.
    if not status.get("server_url"):
        lines.append(t("cli.daemon.status_conn_local"))
    elif status.get("connected"):
        lines.append(t("cli.daemon.status_conn_connected"))
    else:
        # Surface the real failure reason. Earlier this fell back to the literal
        # "not connected" when last_error was empty, rendering the useless
        # "Connection: not connected (not connected)"; an empty reason now drops
        # the parenthetical entirely and points at the log instead of repeating
        # an information-free literal.
        reason = (status.get("last_error") or "").strip()
        if reason:
            lines.append(t("cli.daemon.status_conn_not_connected_reason", reason=reason))
        else:
            lines.append(t("cli.daemon.status_conn_not_connected_no_reason"))
    tracked = status.get("tracked_flows") or []
    lines.append(t("cli.daemon.status_tracked_flows", count=len(tracked)))
    for rec in tracked:
        flow_id = rec.get("flow_id") or t("cli.daemon.flow_unknown")
        lines.append(t("cli.daemon.status_flow_entry", pid=rec.get('pid'), flow_id=flow_id, origin=rec.get('origin')))
    render_text("\n".join(lines), title=t("cli.daemon.status_title"))
    raise typer.Exit(0)


app.add_typer(daemon_app, name="daemon", help=t("cli.help.daemon"))


@app.command(name="merge", help=t("cli.help.merge.desc"))
def merge_cmd(
    branches: list[str] = typer.Argument(None, help=t("cli.help.merge.branches")),
    strategy: str = typer.Option(None, "--strategy", "-s", help=t("cli.help.merge.strategy")),
    delete_merged: bool = typer.Option(None, "--delete-merged", "-d", help=t("cli.help.merge.delete_merged")),
    no_delete_merged: bool = typer.Option(False, "--no-delete-merged", help=t("cli.help.merge.no_delete_merged")),
):
    """Merge one or more branches sequentially into the current branch.

    A thin adapter over the merge library: it runs ``integrate()`` (branch
    merges + conflict resolution + runtime sync + issue renumber + post-condition
    checks) back-to-back with ``reconcile()`` (the merge-side version release
    point — the final version is derived from the merged-in session intents
    against master's current version, never a version a session guessed).

    The CLI has no confirmation gate: failure is expressed via a non-zero exit
    code and the operator reruns the whole command (integrate is then a no-op —
    the branches are already ancestors — and reconcile idempotently re-attempts
    only the still-outstanding version decision). No ``tianluo/calls/`` files are
    written on this path; an escalation surfaces in the rendered output.

    Default strategy and delete-merged behaviour can be configured in
    tianluo.yaml under the ``merge:`` section.

    Examples:
        luo merge feature-x
        luo merge feature-x feature-y --strategy=strict
        luo merge feature-x --delete-merged
    """
    from .commands.merge_cmd import run_merge, validate_branch_names
    from .commands.run import get_project_root
    from .config import load_merge_config

    # Resolve project root first so config is loaded from the correct location
    # (not cwd, which may be a subdirectory of the project). This also binds the
    # i18n language to the project, so it must precede every t()-rendered
    # argument-validation error below — otherwise those errors alone would speak
    # the cwd-resolved language while the rest of the output follows the project.
    project_root = get_project_root()

    # Defect I1: empty branch list must be rejected at CLI input level so a
    # silent zero-iteration "success" is impossible. typer.BadParameter renders
    # the standard click-style error including --help hint.
    if not branches:
        raise typer.BadParameter(
            t("cli.merge.branch_required"),
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
        # CLI wrapper semantics (change C): no confirmation gate, no self-written
        # human-call files — the orchestrator records escalations on the result
        # and the non-zero exit code drives recovery (rerun the whole command).
        suppress_human_call=True,
    )
    raise typer.Exit(exit_code)


@app.command(name="merge-respond", help=t("cli.help.merge_respond.desc"))
def merge_respond_cmd(
    call_file: Path = typer.Argument(..., help=t("cli.help.merge_respond.call_file")),
):
    """Process an MCP call response file for merge conflicts.

    After editing the .response file for a merge call, run this command
    to execute the conflict decisions (accept LLM resolution, abort, or manual).

    Example:
        luo merge-respond tianluo/calls/merge_20240101_120000_feat-x.json
    """
    from .commands.merge_respond import process_merge_response

    exit_code = process_merge_response(call_file)
    raise typer.Exit(exit_code)


@app.command(name="merge-unlock", help=t("cli.help.merge_unlock.desc"))
def merge_unlock_cmd(
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help=t("cli.help.merge_unlock.force"),
    ),
):
    """Manually release (and inspect) the current project's merge lock.

    Always reports the holder PID, whether it is alive / stale, and the
    absolute lock-file path. A stale lock (holder process dead, no PID
    recorded, or a corrupt record) is cleaned up automatically. A lock held
    by a live process is refused (exit 1) unless --force is given.

    flock binds the kernel lock to the holding process's fd, so this command
    cannot truly revoke a foreign lock — it removes the lock file so the next
    acquirer recreates it, matching how stale locks are reclaimed.

    Examples:
        luo merge-unlock          # inspect + clean up a stale lock
        luo merge-unlock --force  # force-release even a live holder
    """
    from .commands.merge.merge_lock import release_merge_lock
    from .commands.run import get_project_root
    from .core.machine_id import is_local_machine

    project_root = get_project_root()
    outcome = release_merge_lock(project_root, force=force)
    status = outcome.status

    # Status report — always printed, including when there is no lock.
    lines = [t("cli.merge_unlock.lock_file", lock_file=status.lock_file)]
    if not status.exists:
        lines.append(t("cli.merge_unlock.holder_none"))
        lines.append(t("cli.merge_unlock.state_no_lock"))
    else:
        pid_str = (
            str(status.holder_pid)
            if status.holder_pid is not None
            else t("cli.merge_unlock.pid_none_recorded")
        )
        lines.append(t("cli.merge_unlock.holder_pid", pid=pid_str))
        if not is_local_machine(status.holder_machine):
            # A foreign holder is reported alive (its PID is unprobeable from
            # here), but the generic "alive" line hides the one fact the
            # operator needs before deciding on --force: which host to check.
            lines.append(
                t(
                    "cli.merge_unlock.state_held_remote",
                    machine=status.holder_machine,
                )
            )
        elif status.corrupt:
            lines.append(t("cli.merge_unlock.state_stale_corrupt"))
        elif status.stale:
            if status.holder_pid is None:
                lines.append(t("cli.merge_unlock.state_stale_no_pid"))
            else:
                lines.append(t("cli.merge_unlock.state_stale_dead"))
        else:
            lines.append(t("cli.merge_unlock.state_alive"))

    if outcome.action == "no_lock":
        lines.append(t("cli.merge_unlock.no_lock_to_release"))
    elif outcome.action == "released_stale":
        lines.append(t("cli.merge_unlock.released_stale"))
    elif outcome.action == "released_force":
        lines.append(t("cli.merge_unlock.released_force"))
        lines.append(t("cli.merge_unlock.released_force_warning"))
    elif outcome.action == "refused_alive":
        lines.append(t("cli.merge_unlock.refused_alive"))
        lines.append(t("cli.merge_unlock.refused_alive_hint"))
    elif outcome.action == "failed_remove":
        lines.append(t("cli.merge_unlock.failed_remove"))
        lines.append(t("cli.merge_unlock.failed_remove_hint"))

    render_text("\n".join(lines), title=t("cli.merge_unlock.title"))
    raise typer.Exit(outcome.exit_code)


@app.command(name="salvage", help=t("cli.help.salvage.desc"))
def salvage_cmd(
    project_root: Optional[str] = typer.Option(None, "--project-root", "-p", help=t("cli.help.common.project_root")),
):
    """Salvage work from an abnormally terminated session.

    Performs best-effort recovery:
    1. Reads session state (tolerant of corruption)
    2. Evaluates git diff
    3. Commits existing changes
    4. Creates issues for unfinished work
    5. Archives the session

    Use this when a session crashed or was interrupted.
    After salvage, use 'luo run --from-issue' to continue work.
    """
    from .commands.salvage_cmd import salvage

    root = Path(project_root) if project_root else None
    bind_project_root(root)
    exit_code = salvage(root)
    raise typer.Exit(exit_code)


@app.command(name="end-session", help=t("cli.help.end_session.desc"))
def end_session_cmd(
    flow_id: Optional[str] = typer.Argument(
        None,
        help=t("cli.help.end_session.flow_id"),
    ),
    project_root: Optional[str] = typer.Option(
        None, "--project-root", "-p", help=t("cli.help.common.project_root")
    ),
    pid: Optional[int] = typer.Option(
        None, "--pid", help=t("cli.help.end_session.pid")
    ),
    no_archive_worktree: bool = typer.Option(
        False,
        "--no-archive-worktree",
        help=t("cli.help.end_session.no_archive_worktree"),
    ),
):
    """End and archive a session (worktree is cleaned up; uncommitted work is NOT merged).

    Terminates the live ``luo run`` process (if any) and archives the session.
    A ``--worktree`` session is archived exactly like a normally completed run —
    the worktree is archived, its terminal state is promoted into the main
    project's archive, its history is synced, and the isolation branch +
    worktree are removed — but its (possibly unfinished) work is NOT merged into
    the main branch. A main-branch session simply has its engine state archived.
    """
    from .commands.end_session_cmd import end_session

    root = Path(project_root) if project_root else None
    bind_project_root(root)
    exit_code = end_session(
        project_root=root,
        flow_id=flow_id,
        pid=pid,
        archive_worktree=not no_archive_worktree,
    )
    raise typer.Exit(exit_code)


if __name__ == "__main__":
    app()
