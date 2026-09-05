"""SE3 Issue command — Manage project issues.

Provides commands to list, show, create, edit, close, and reset issues.

Usage:
    luo issue                              # List open issues
    luo issue list                         # List open issues
    luo issue list --all                   # List all issues (including closed)
    luo issue list --source human          # Filter by source
    luo issue list --type bug              # Filter by type
    luo issue show <id>                    # Show issue details
    luo issue create "description"         # Create with positional description
    luo issue create                       # Create interactively (single prompt)
    luo issue create --editor              # Create via external editor
    luo issue edit <id>                    # Edit issue in external editor
    luo issue close <id> [--reason TEXT]   # Close an issue
    luo issue reset <id>                   # Reset in-progress issue to open
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import typer
import yaml
from rich.console import Console
from rich.table import Table

from ..engine.display import render_block_footer, render_block_header
from ..engine.issue_manager import KNOWN_TYPES, IssueManager, IssueStatus
from ..i18n import t, t_status

app = typer.Typer(help=t("cli.help.issue"))
console = Console()


def get_project_root() -> Path:
    """Find project root by looking for .git directory or an SE3 config file.

    Binds the i18n language to the discovered root: the import-time help strings
    resolve the language singleton from the cwd, which can sit below the project
    root, so it must be re-resolved once the target project is known.
    """
    from ..config import is_se3_project_root
    from ..i18n import bind_project_root

    cwd = Path.cwd()
    root = cwd
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".git").exists() or is_se3_project_root(parent):
            root = parent
            break
    bind_project_root(root)
    return root


def _status_color(status: IssueStatus) -> str:
    """Get color for issue status."""
    return {
        IssueStatus.OPEN: "yellow",
        IssueStatus.IN_PROGRESS: "blue",
        IssueStatus.RESOLVED: "green",
        IssueStatus.WONT_FIX: "dim",
        IssueStatus.CLOSED: "green",
    }.get(status, "white")


def _priority_color(priority: Optional[str]) -> str:
    """Get color for priority."""
    if not priority:
        return "white"
    return {
        "critical": "red bold",
        "high": "red",
        "medium": "yellow",
        "low": "dim",
    }.get(priority.lower(), "white")


def _type_color(issue_type: Optional[str]) -> str:
    """Get color for issue type."""
    if not issue_type:
        return "white"
    return {
        "bug": "red",
        "feature": "green",
        "enhancement": "cyan",
        "idea": "magenta",
        "task": "yellow",
    }.get(issue_type.lower(), "white")


def _format_datetime(dt) -> str:
    """Format datetime to human-readable string."""
    try:
        return dt.strftime("%Y-%m-%d %H:%M")
    except (AttributeError, ValueError):
        return str(dt)


def _get_editor() -> str:
    """Return the editor command from $EDITOR or fall back to 'vi'."""
    import shlex

    editor = os.environ.get("EDITOR", "").strip()
    if not editor:
        return "vi"
    # Guard against $EDITOR set to a value that shlex.split yields empty argv
    # or that contains unmatched quotes (ValueError).
    try:
        if not shlex.split(editor):
            return "vi"
    except ValueError:
        return "vi"
    return editor


class EditorError(Exception):
    """Raised when the external editor cannot be launched."""


def _open_editor_with_content(content: str) -> Optional[str]:
    """Open an external editor with the given content and return the edited text.

    Returns None if the editor exits with a non-zero code (user cancelled).
    Raises :class:`EditorError` if the editor binary cannot be found or
    launched.
    """
    import shlex

    editor_str = _get_editor()
    try:
        editor_argv = shlex.split(editor_str)
    except ValueError:
        editor_argv = [editor_str]
    with tempfile.NamedTemporaryFile(
        suffix=".yaml", mode="w", encoding="utf-8", delete=False
    ) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        try:
            result = subprocess.run(editor_argv + [tmp_path])
        except FileNotFoundError:
            raise EditorError(
                t("issue.editor_not_found", editor=repr(editor_argv[0]))
            )
        except OSError as e:
            raise EditorError(t("issue.editor_launch_failed", error=e))
        if result.returncode != 0:
            return None
        edited = Path(tmp_path).read_text(encoding="utf-8")
        return edited
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# YAML template helpers for editor workflows
# ---------------------------------------------------------------------------

def _issue_to_editor_yaml(issue) -> str:
    """Render an Issue as a human-editable YAML template (for edit).

    Only editable fields are included (id is shown as read-only context).
    Fields like status and source are excluded so the user is not
    invited to edit values that would be silently discarded.
    """
    data = issue.to_dict()
    # Only include editable fields plus id (read-only context).
    ordered = {}
    for key in ["id", "title", "description", "priority", "type", "tags"]:
        if key in data:
            ordered[key] = data[key]
    return yaml.dump(ordered, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _new_issue_editor_yaml() -> str:
    """Generate a blank YAML template for creating a new issue via editor."""
    template = {
        "title": "",
        "description": "",
        "type": "",
        "priority": "",
        "tags": [],
    }
    return yaml.dump(template, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _parse_edited_issue_yaml(text: str) -> dict:
    """Parse edited YAML and validate required fields.

    Returns the parsed dict. Raises ValueError on invalid YAML or missing
    description.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(t("issue.invalid_yaml", error=e)) from e
    if not data or not isinstance(data, dict):
        raise ValueError(t("issue.invalid_yaml_empty"))
    desc = data.get("description", "")
    if not desc or not str(desc).strip():
        raise ValueError(t("issue.desc_empty_yaml"))
    return data


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback(invoke_without_command=True, help=t("cli.help.issue.default.desc"))
def default_cmd(ctx: typer.Context):
    """List open issues (default command)."""
    if ctx.invoked_subcommand is not None:
        return
    list_cmd(show_all=False, type_filter=None, source_filter=None)


@app.command(name="list", help=t("cli.help.issue.list.desc"))
def list_cmd(
    show_all: bool = typer.Option(False, "--all", "-a", help=t("cli.help.issue.list.all")),
    type_filter: Optional[str] = typer.Option(None, "--type", "-t", help=t("cli.help.issue.list.type")),
    source_filter: Optional[str] = typer.Option(None, "--source", help=t("cli.help.issue.list.source")),
):
    """List issues."""
    VALID_SOURCES = {"human", "system"}
    if source_filter is not None and source_filter not in VALID_SOURCES:
        raise typer.BadParameter(
            t("issue.list.invalid_source", source=source_filter),
            param_hint="--source",
        )
    project_root = get_project_root()
    mgr = IssueManager(project_root)
    issues = mgr.list_issues(include_closed=show_all, type_filter=type_filter, source_filter=source_filter)

    if not issues:
        if show_all:
            typer.echo(t("issue.list.none_all"))
        else:
            typer.echo(t("issue.list.none_open"))
        return

    title = t("issue.list.title_all") if show_all else t("issue.list.title_open")
    table = Table(title=title)
    table.add_column(t("issue.col.id"), style="cyan", no_wrap=True)
    table.add_column(t("issue.col.title"), style="white")
    table.add_column(t("issue.col.type"))
    table.add_column(t("issue.col.status"))
    table.add_column(t("issue.col.priority"))
    table.add_column(t("issue.col.source"), style="dim")
    table.add_column(t("issue.col.tags"), style="dim")
    table.add_column(t("issue.col.created"), style="dim")

    for issue in issues:
        sc = _status_color(issue.status)
        pc = _priority_color(issue.priority)
        tc = _type_color(issue.type)
        tags_str = ", ".join(issue.tags) if issue.tags else ""
        title_str = issue.display_title
        if len(title_str) > 50:
            title_str = title_str[:50] + "..."
        priority_str = issue.priority or "-"
        type_str = issue.type or "-"

        table.add_row(
            issue.id,
            title_str,
            f"[{tc}]{type_str}[/{tc}]",
            f"[{sc}]{t_status(issue.status)}[/{sc}]",
            f"[{pc}]{priority_str}[/{pc}]",
            issue.source,
            tags_str,
            _format_datetime(issue.created_at),
        )

    console.print(table)


@app.command(name="show", help=t("cli.help.issue.show.desc"))
def show_cmd(
    issue_id: str = typer.Argument(..., help=t("cli.help.issue.show.issue_id")),
):
    """Show detailed information about an issue."""
    project_root = get_project_root()
    mgr = IssueManager(project_root)
    issue = mgr.load(issue_id)

    if not issue:
        typer.echo(t("issue.not_found", issue_id=issue_id), err=True)
        raise typer.Exit(1)

    sc = _status_color(issue.status)
    pc = _priority_color(issue.priority)
    tc = _type_color(issue.type)
    tags_str = ", ".join(issue.tags) if issue.tags else t("issue.show.tags_none")
    priority_str = issue.priority or "-"
    type_str = issue.type or "-"

    content = t(
        "issue.show.detail",
        title=issue.display_title,
        tc=tc,
        type=type_str,
        sc=sc,
        status=t_status(issue.status),
        pc=pc,
        priority=priority_str,
        source=issue.source,
        tags=tags_str,
        created=_format_datetime(issue.created_at),
        updated=_format_datetime(issue.updated_at),
        description=issue.description,
    )

    render_block_header(t("issue.show.header", id=issue.id), "cyan")
    console.print(content)
    console.print()
    render_block_footer("cyan")


@app.command(name="create", help=t("cli.help.issue.create.desc"))
def create_cmd(
    description_arg: Optional[str] = typer.Argument(None, help=t("cli.help.issue.create.description")),
    title: Optional[str] = typer.Option(None, "--title", help=t("cli.help.issue.create.title")),
    issue_type: Optional[str] = typer.Option(None, "--type", help=t("cli.help.issue.create.type")),
    priority: Optional[str] = typer.Option(None, "--priority", help=t("cli.help.issue.create.priority")),
    tags: Optional[str] = typer.Option(None, "--tags", help=t("cli.help.issue.create.tags")),
    use_editor: bool = typer.Option(False, "--editor", help=t("cli.help.issue.create.editor")),
):
    """Create a new issue.

    Three input modes:
      1. Positional:  luo issue create "my description"
      2. Piped stdin: echo "desc" | luo issue create
      3. Interactive: luo issue create  (single description prompt via _read_multiline_input)

    Use --editor to open $EDITOR with a full YAML template.
    """
    project_root = get_project_root()
    mgr = IssueManager(project_root)

    # --- Editor mode ---
    if use_editor:
        template = _new_issue_editor_yaml()
        try:
            edited = _open_editor_with_content(template)
        except EditorError as e:
            typer.echo(f"{t('cli.common.error')}: {e}", err=True)
            raise typer.Exit(1)
        if edited is None:
            typer.echo(t("issue.cancelled"), err=True)
            raise typer.Exit(1)
        try:
            data = _parse_edited_issue_yaml(edited)
        except ValueError as e:
            typer.echo(f"{t('cli.common.error')}: {e}", err=True)
            raise typer.Exit(1)

        desc = str(data.get("description", "")).strip()
        iss_title = data.get("title") or None
        iss_type = data.get("type") or None
        iss_priority = data.get("priority") or None
        iss_tags = data.get("tags", [])
        if isinstance(iss_tags, str):
            iss_tags = [t.strip() for t in iss_tags.split(",") if t.strip()]
        elif not isinstance(iss_tags, list):
            iss_tags = []

        issue = mgr.create(
            description=desc,
            title=str(iss_title).strip() if iss_title else None,
            priority=str(iss_priority).strip() if iss_priority else None,
            tags=iss_tags,
            type=str(iss_type).strip() if iss_type else None,
            source="human",
        )
        typer.echo(t("issue.created", id=issue.id, title=issue.display_title))
        return

    # --- Resolve description from arg, stdin pipe, or interactive prompt ---
    try:
        description = _resolve_description(description_arg)
    except ValueError as e:
        typer.echo(f"{t('cli.common.error')}: {e}", err=True)
        raise typer.Exit(1)

    if description is None:
        typer.echo(t("issue.cancelled"), err=True)
        raise typer.Exit(1)

    # Parse tags
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    issue = mgr.create(
        description=description,
        title=title,
        priority=priority,
        tags=tag_list,
        type=issue_type,
        source="human",
    )
    typer.echo(t("issue.created", id=issue.id, title=issue.display_title))


def _resolve_description(positional: Optional[str]) -> Optional[str]:
    """Resolve the description from positional arg, piped stdin, or interactive prompt.

    Returns None if the user cancels (Ctrl+C).
    Raises ValueError if the resolved input is empty or whitespace-only.
    """
    # 1. Positional argument takes priority
    if positional:
        stripped = positional.strip()
        if stripped:
            return stripped
        raise ValueError(t("issue.desc_empty"))

    # 2. Piped stdin (non-TTY): read all of stdin. Reading the fd directly is
    # safe here and only here: ``luo issue`` is its own command with a single
    # stdin consumer, so the shared funnel that ``luo run`` needs (several
    # consumers over one held-open pipe) has nothing to arbitrate.
    if not sys.stdin.isatty():
        try:
            content = sys.stdin.read()
            if content:
                stripped = content.strip()
                if stripped:
                    return stripped
        except (EOFError, KeyboardInterrupt):
            return None
        raise ValueError(t("issue.desc_empty"))

    # 3. Interactive TTY: single description prompt
    from ..cli import _read_multiline_input

    description = _read_multiline_input(
        prompt_title=t("issue.create.prompt_title"),
        prompt_message=t("issue.create.prompt_message"),
    )
    if description is None:
        return None  # user cancelled
    if not description:
        return None  # empty input
    return description


@app.command(name="edit", help=t("cli.help.issue.edit.desc"))
def edit_cmd(
    issue_id: str = typer.Argument(..., help=t("cli.help.issue.edit.issue_id")),
):
    """Edit an issue in an external editor ($EDITOR, fallback vi)."""
    project_root = get_project_root()
    mgr = IssueManager(project_root)
    issue = mgr.load(issue_id)

    if not issue:
        typer.echo(t("issue.not_found", issue_id=issue_id), err=True)
        raise typer.Exit(1)

    template = _issue_to_editor_yaml(issue)
    try:
        edited = _open_editor_with_content(template)
    except EditorError as e:
        typer.echo(f"{t('cli.common.error')}: {e}", err=True)
        raise typer.Exit(1)
    if edited is None:
        typer.echo(t("issue.edit.cancelled"), err=True)
        raise typer.Exit(1)

    try:
        data = _parse_edited_issue_yaml(edited)
    except ValueError as e:
        typer.echo(f"{t('cli.common.error')}: {e}", err=True)
        raise typer.Exit(1)

    # Apply changes via update_fields.
    # The edited YAML is authoritative: a field the user removed from the
    # template is treated as "clear to default", not "leave unchanged".
    # We always pass every editable field (defaulting missing keys to empty
    # string / empty list) so that update_fields receives them and applies
    # the clearing semantics (e.g. title="" → None).
    try:
        iss_title = str(data.get("title") or "").strip()
        iss_desc = str(data.get("description", "")).strip()
        iss_priority = str(data.get("priority") or "").strip()
        iss_type = str(data.get("type") or "").strip()
        iss_tags = data.get("tags")
        if isinstance(iss_tags, str):
            iss_tags = [t.strip() for t in iss_tags.split(",") if t.strip()]
        elif not isinstance(iss_tags, list):
            iss_tags = []

        updated = mgr.update_fields(
            issue_id=issue.id,
            title=iss_title,
            description=iss_desc,
            priority=iss_priority,
            type=iss_type,
            tags=iss_tags,
        )
        typer.echo(t("issue.updated", id=updated.id, title=updated.display_title))
    except ValueError as e:
        typer.echo(f"{t('cli.common.error')}: {e}", err=True)
        raise typer.Exit(1)


@app.command(name="close", help=t("cli.help.issue.close.desc"))
def close_cmd(
    issue_id: str = typer.Argument(..., help=t("cli.help.issue.close.issue_id")),
    reason: Optional[str] = typer.Option(None, "--reason", help=t("cli.help.issue.close.reason")),
):
    """Close an issue."""
    project_root = get_project_root()
    mgr = IssueManager(project_root)

    try:
        issue = mgr.close_issue(issue_id, reason=reason or "")
        typer.echo(t("issue.closed", id=issue.id, title=issue.display_title))
    except ValueError as e:
        typer.echo(f"{t('cli.common.error')}: {e}", err=True)
        raise typer.Exit(1)


@app.command(name="reset", help=t("cli.help.issue.reset.desc"))
def reset_cmd(
    issue_id: str = typer.Argument(..., help=t("cli.help.issue.reset.issue_id")),
):
    """Reset an in-progress issue back to open."""
    project_root = get_project_root()
    mgr = IssueManager(project_root)

    try:
        issue = mgr.reset_to_open(issue_id)
        typer.echo(t("issue.reset", id=issue.id))
    except ValueError as e:
        typer.echo(f"{t('cli.common.error')}: {e}", err=True)
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
