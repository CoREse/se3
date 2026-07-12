"""SE3 ``worktree`` command group — operator surface for isolation worktrees.

Today it carries a single subcommand, ``gc``, the manual trigger face for the
worktree garbage collector (:mod:`se3.engine.merge.worktree_gc`). The GC core
lives in the engine layer so both trigger surfaces — this CLI command and the
daemon's periodic task — drive the exact same reclamation logic; this module is
only the thin render/exit-code shell around it.

``se3 worktree gc`` reclaims leaked ``se3 run --worktree`` runs (terminal +
idle worktrees stranded under ``se3/worktrees/`` when a paused-then-resumed or
hand-merged flow never runs the finalize/merge cleanup). The rendered report is
deliberately three-part so an operator can see, at a glance: what was archived
and how much space it freed, which unmerged branches were KEPT (the core safety
promise — no unmerged work is ever silently deleted), and what was skipped or
errored.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from ..engine.merge.worktree_gc import gc_worktree_runs
from ..i18n import t

console = Console()


def _format_bytes(num: int) -> str:
    """Render a byte count as a compact human-readable size (e.g. ``50.0 MB``).

    Binary (1024) units are used because the figure describes on-disk space
    reclaimed, which filesystems report in KiB/MiB.
    """
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


worktree_app = typer.Typer(
    name="worktree",
    help=t("cli.help.worktree"),
)


@worktree_app.command(name="gc", help=t("cli.help.worktree.gc.desc"))
def gc_command(
    max_age_hours: float = typer.Option(
        24.0,
        "--max-age-hours",
        help=t("cli.help.worktree.gc.max_age_hours"),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=t("cli.help.worktree.gc.dry_run"),
    ),
    project_root: Optional[str] = typer.Option(
        None,
        "--project-root",
        "-p",
        help=t("cli.help.worktree.gc.project_root"),
    ),
) -> None:
    """Garbage-collect leaked terminal ``se3 run --worktree`` runs.

    Enumerates worktree runs under ``se3/worktrees/`` whose engine.json is in a
    terminal state (COMPLETED / FAILED) and has been idle at least
    ``--max-age-hours``, then per run: archives it into
    ``se3/worktrees/.archive/``, promotes its terminal state into the main
    archive, removes the worktree, and — ONLY when the branch is provably
    merged — deletes the branch. An unmerged branch's ref is ALWAYS kept and
    surfaced with a loud warning so no unmerged work is silently lost.

    Exits non-zero if any run errored (so scripted/cron callers can detect a
    partial sweep); a clean or empty sweep exits 0.

    Examples:
        se3 worktree gc --dry-run
        se3 worktree gc --max-age-hours 48
    """
    if project_root:
        from ..i18n import bind_project_root

        root = Path(project_root)
        # get_project_root() binds the UI language itself; an explicit
        # --project-root bypasses it, so bind here too.
        bind_project_root(root)
    else:
        from .run import get_project_root

        root = get_project_root()

    report = gc_worktree_runs(
        root,
        max_age_seconds=max_age_hours * 3600.0,
        dry_run=dry_run,
    )

    mode = t("worktree.gc.dry_run_prefix") if dry_run else ""
    console.print()
    console.print(
        t("worktree.gc.header", mode=mode, root=root, hours=max_age_hours)
    )

    # --- Section 1: archived + reclaimed space --------------------------------
    if report.archived:
        table = Table(
            title=(
                t("worktree.gc.archived_title_dry")
                if dry_run
                else t("worktree.gc.archived_title")
            ),
        )
        table.add_column(t("worktree.gc.col_worktree"), style="cyan")
        table.add_column(t("worktree.gc.col_archive_path"))
        table.add_column(t("worktree.gc.col_size"), justify="right")
        for name, archive_path, size in report.archived:
            table.add_row(
                name,
                str(archive_path) if archive_path else t("worktree.gc.cell_dry_run"),
                _format_bytes(size),
            )
        console.print()
        console.print(table)
    else:
        console.print()
        console.print(t("worktree.gc.no_matches"))

    console.print(
        t("worktree.gc.reclaimed", size=_format_bytes(report.reclaimed_bytes))
        + (t("worktree.gc.projected") if dry_run else "")
    )

    # --- Section 2: retained unmerged branches (the safety promise) -----------
    if report.retained_unmerged:
        console.print()
        console.print(t("worktree.gc.retained_warning"))
        console.print(t("worktree.gc.retained_detail"))
        table = Table(title=t("worktree.gc.retained_title"))
        table.add_column(t("worktree.gc.col_branch"), style="yellow")
        table.add_column(t("worktree.gc.col_original_branch"))
        table.add_column(t("worktree.gc.col_reason"))
        for branch, original, reason in report.retained_unmerged:
            table.add_row(branch, original or t("worktree.gc.cell_unknown"), reason)
        console.print(table)

    # --- Section 3: skipped + errors ------------------------------------------
    if report.skipped:
        table = Table(title=t("worktree.gc.skipped_title"))
        table.add_column(t("worktree.gc.col_worktree"), style="dim")
        table.add_column(t("worktree.gc.col_reason"))
        for name, reason in report.skipped:
            table.add_row(name, reason)
        console.print()
        console.print(table)

    if report.errors:
        table = Table(title=t("worktree.gc.errors_title"))
        table.add_column(t("worktree.gc.col_worktree"), style="red")
        table.add_column(t("worktree.gc.col_reason"))
        for name, reason in report.errors:
            table.add_row(name, reason)
        console.print()
        console.print(table)

    console.print()
    raise typer.Exit(1 if report.errors else 0)
