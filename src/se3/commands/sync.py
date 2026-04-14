"""SE3 Sync command — Check and synchronize specs with project code.

Usage:
    se3 sync                    # Sync with default mode
    se3 sync --mode=strict      # All conflicts require human decision
    se3 sync --mode=fast        # LLM handles all conflicts automatically
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from pathlib import Path
from typing import Optional

import typer
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..engine.display import get_console, render_text

logger = logging.getLogger(__name__)


class SyncMode(str, Enum):
    """Conflict handling mode for sync."""

    DEFAULT = "default"
    STRICT = "strict"
    FAST = "fast"


def _render_sync_results(result) -> None:
    """Render sync results using Rich Table and Panel."""
    from ..engine.sync_engine import SyncResult

    console = get_console()

    table = Table(title="Spec Status Overview", expand=True)
    table.add_column("Spec", style="bold")
    table.add_column("Gaps", justify="right")
    table.add_column("Extensions", justify="right")
    table.add_column("Conflicts", justify="right")
    table.add_column("Status", justify="center")

    for analysis in result.analyses:
        gaps = len(analysis.gaps)
        exts = len(analysis.extensions)
        confs = len(analysis.conflicts)

        if analysis.is_in_sync:
            status = Text("In Sync", style="green")
        else:
            status = Text("Diffs Found", style="yellow")

        table.add_row(
            analysis.spec_name,
            str(gaps) if gaps else "-",
            str(exts) if exts else "-",
            str(confs) if confs else "-",
            status,
        )

    console.print(table)

    summary_parts = []
    summary_parts.append(f"Issues created: {result.issues_created}")
    summary_parts.append(f"Issues closed:  {result.issues_closed}")
    summary_parts.append(f"Specs updated:  {result.specs_updated}")
    summary_parts.append(f"Conflicts pending: {len(result.conflicts)}")

    if result.all_in_sync and not result.conflicts:
        border_style = "green"
        title = "Sync Complete — All In Sync"
    elif result.conflicts:
        border_style = "yellow"
        title = "Sync Complete — Action Required"
    else:
        border_style = "blue"
        title = "Sync Complete"

    summary_text = "\n".join(summary_parts)

    if result.call_file:
        summary_text += (
            f"\n\nMCP call file created: {result.call_file}"
            f"\nRespond via: se3 sync-respond '{result.call_file}'"
        )

    console.print(Panel(summary_text, title=title, border_style=border_style))


def sync_command(
    mode: SyncMode = SyncMode.DEFAULT,
    project_root: Path | None = None,
) -> None:
    """Check and synchronize se3/specs/ with project code.

    Args:
        mode: Conflict handling mode.
        project_root: Project root directory. Auto-detected if None.
    """
    from ..engine.sync_engine import SyncEngine

    if project_root is None:
        from .run import get_project_root
        project_root = get_project_root()

    console = get_console()
    console.print(
        Panel(
            f"Mode: [bold]{mode.value}[/bold]\nProject: {project_root}",
            title="SE3 Sync",
            border_style="blue",
        )
    )

    logger.info("se3 sync called with mode=%s, project_root=%s", mode.value, project_root)

    engine = SyncEngine(project_root, mode=mode.value)

    def on_progress(phase, spec_name, index, total, analysis):
        if phase == "analyzed" and analysis is not None:
            gaps = len(analysis.gaps)
            exts = len(analysis.extensions)
            confs = len(analysis.conflicts)
            if analysis.is_in_sync:
                console.print(f"  [green]✓[/green] {spec_name}: in sync")
            else:
                parts = []
                if gaps:
                    parts.append(f"{gaps} gap(s)")
                if exts:
                    parts.append(f"{exts} extension(s)")
                if confs:
                    parts.append(f"{confs} conflict(s)")
                console.print(f"  [yellow]△[/yellow] {spec_name}: {', '.join(parts)}")

    with console.status("[bold blue]Analyzing specs...[/bold blue]") as status:
        def progress_with_status(phase, spec_name, index, total, analysis):
            if phase == "analyzing":
                status.update(
                    f"[bold blue]Analyzing ({index + 1}/{total}): {spec_name}[/bold blue]"
                )
            else:
                on_progress(phase, spec_name, index, total, analysis)

        result = engine.run(progress_callback=progress_with_status)

    console.print()
    _render_sync_results(result)


def process_call_response(
    call_file: Path,
    project_root: Optional[Path] = None,
) -> None:
    """Process an MCP call response file for sync conflicts.

    Scans the response file for the given call file, parses each
    conflict's decision, and executes the corresponding action.

    Args:
        call_file: Path to the original call file.
        project_root: Project root directory. Auto-detected if None.
    """
    from ..engine.sync_engine import SyncEngine

    if project_root is None:
        from .run import get_project_root
        project_root = get_project_root()

    call_path = Path(call_file)
    if not call_path.exists():
        render_text(f"Call file not found: {call_path}", title="SE3 Sync Error")
        return

    response_path = Path(str(call_path) + ".response")
    if not response_path.exists():
        render_text(f"Response file not found: {response_path}", title="SE3 Sync Error")
        return

    engine = SyncEngine(project_root)
    result = engine.process_call_response(call_path)

    render_text(
        f"Specs updated: {result['specs_updated']}\n"
        f"Issues created: {result['issues_created']}",
        title="SE3 Sync — Call Response Processed",
    )
