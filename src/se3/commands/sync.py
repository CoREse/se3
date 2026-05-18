"""SE3 Sync command — Run code → spec sync until convergence.

The CLI surface for ``se3 sync`` drives :class:`SyncLoop` and renders
per-round progress plus a final convergence / oscillation / max-rounds
report. Specs are treated as the documented snapshot of code
(spec-assistant); this command never modifies code, only specs.

Usage:
    se3 sync
    se3 sync --once
    se3 sync --max-rounds 5 --stable-rounds 2
    se3 sync --interactive
    se3 sync --show-diff
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from rich.table import Table
from rich.tree import Tree

from ..engine.display import (
    get_console,
    render_block_footer,
    render_block_header,
    render_text,
)
from ..engine.spec_validator import validate_spec_structure

logger = logging.getLogger(__name__)


CONVERGENCE_DISCLAIMER = (
    "Note: Convergence means the LLM found no further drift in the last "
    "round; it does not guarantee absolute spec-code consistency."
)


def _render_round_changes(console, loop_result) -> None:
    """Render a Rich tree of changes grouped by round (for --show-diff)."""
    rounds = getattr(loop_result, "rounds", []) or []
    has_changes = any(getattr(r, "changes_by_spec", None) for r in rounds)
    if not has_changes:
        return

    tree = Tree("[bold]Per-Round Changes[/bold]")
    for r in rounds:
        changes_by_spec = dict(getattr(r, "changes_by_spec", {}) or {})
        if not changes_by_spec:
            continue
        round_node = tree.add(
            f"[cyan]Round {r.round_index}[/cyan] "
            f"({r.specs_updated} spec(s) updated)"
        )
        for spec_name, descs in changes_by_spec.items():
            spec_node = round_node.add(f"[bold]{spec_name}[/bold]")
            for d in descs:
                spec_node.add(d)

    render_block_header("Spec Diff", "cyan")
    console.print(tree)
    console.print("")
    render_block_footer("cyan")


def _format_round_line(round_index: int, changes_by_spec) -> str:
    """One-line round summary, e.g. ``Round 2: 3 specs updated (a.md, b.md)``."""
    names = list((changes_by_spec or {}).keys())
    count = len(names)
    if count == 0:
        return f"Round {round_index}: 0 specs updated"
    display = ", ".join(names[:5])
    if count > 5:
        display += f", … ({count - 5} more)"
    return f"Round {round_index}: {count} spec(s) updated ({display})"


def _render_loop_result(loop_result, show_diff: bool) -> None:
    """Render the final report for a SyncLoop run.

    Branches:

    * converged → green "Sync Converged" header
    * oscillation_detected → red "Sync Aborted — Oscillation Detected"
    * else (max_rounds exhausted) → yellow "Sync Did Not Converge"

    In all branches the disclaimer string ``CONVERGENCE_DISCLAIMER`` is
    printed verbatim so that scripted callers / tests can match it.
    """
    console = get_console()

    if show_diff:
        _render_round_changes(console, loop_result)

    final_round = getattr(loop_result, "final_round_index", 0) or 0
    total_specs_updated = getattr(loop_result, "total_specs_updated", 0) or 0
    rounds = getattr(loop_result, "rounds", []) or []
    final_round_changes = rounds[-1].specs_updated if rounds else 0

    converged = bool(getattr(loop_result, "converged", False))
    oscillation = bool(getattr(loop_result, "oscillation_detected", False))
    paused = bool(getattr(loop_result, "paused", False))

    level_1_cache_hit = bool(getattr(loop_result, "level_1_cache_hit", False))

    if converged and level_1_cache_hit:
        title = "Sync — Already In Sync"
        color = "green"
        summary_line = (
            "in-sync (0 LLM calls). The code fingerprint is unchanged "
            "since the last converged sync; the global shutter skipped "
            "every spec. Nothing to update."
        )
    elif converged:
        title = "Sync Converged"
        color = "green"
        summary_line = (
            f"Converged after {final_round} round(s). "
            f"Total {total_specs_updated} spec(s) updated. "
            f"Final round: {final_round_changes} change(s)."
        )
    elif oscillation:
        title = "Sync Aborted — Oscillation Detected"
        color = "red"
        report = getattr(loop_result, "oscillation_report", None) or ""
        summary_line = (
            f"Oscillation detected, aborted after {final_round} round(s). "
            f"Total {total_specs_updated} spec(s) updated. "
            f"{report}"
        )
    elif paused:
        title = "Sync Paused — Checkpoint Saved"
        color = "yellow"
        cp_path = getattr(loop_result, "checkpoint_path", None) or (
            "se3/state/sync_checkpoint.json"
        )
        summary_line = (
            f"Paused after {final_round} round(s) due to sustained "
            f"infrastructure failures. "
            f"Total {total_specs_updated} spec(s) updated. "
            f"Checkpoint saved at {cp_path}. "
            f"Re-run with `se3 sync --resume` once the quota / "
            f"infrastructure recovers."
        )
    else:
        title = "Sync Did Not Converge"
        color = "yellow"
        summary_line = (
            f"Did not converge within {final_round} round(s). "
            f"Total {total_specs_updated} spec(s) updated. "
            f"Increase --max-rounds or rerun to continue."
        )

    if getattr(loop_result, "total_specs_created", None):
        created = list(loop_result.total_specs_created)
        if created:
            summary_line += f"\nNew specs created: {', '.join(created)}"

    high_impact_total = 0
    for r in rounds:
        high_impact_total += len(getattr(r, "high_impact_deletions", []) or [])
    if high_impact_total:
        summary_line += (
            f"\nHigh-impact deletions processed: {high_impact_total}"
        )

    # Incremental-skip telemetry (G7). The level-1 branch already states
    # "0 LLM calls"; only report level 2/3 for the non-shutter paths.
    if not level_1_cache_hit:
        level_2_skipped = list(
            getattr(loop_result, "level_2_skipped_specs", []) or []
        )
        if level_2_skipped:
            summary_line += (
                f"\nLevel-2 cache: {len(level_2_skipped)} spec(s) skipped "
                f"for the whole run ({', '.join(level_2_skipped)})"
            )
        level_3_early = list(
            getattr(loop_result, "level_3_early_exit_specs", []) or []
        )
        if level_3_early:
            summary_line += (
                f"\nLevel-3 early exit: {len(level_3_early)} spec(s) "
                f"converged and left the round loop early "
                f"({', '.join(level_3_early)})"
            )

    if getattr(loop_result, "obsolete_specs_deleted", None):
        deleted_specs = list(loop_result.obsolete_specs_deleted)
        if deleted_specs:
            summary_line += (
                f"\nObsolete specs deleted: {', '.join(deleted_specs)}"
            )

    if getattr(loop_result, "obsolete_specs_kept", None):
        kept_specs = list(loop_result.obsolete_specs_kept)
        if kept_specs:
            summary_line += (
                f"\nObsolete specs kept: {', '.join(kept_specs)}"
            )

    if getattr(loop_result, "discovery_failed", False):
        summary_line += (
            "\n[yellow]Warning: spec discovery failed during round 1; "
            "newly-uncovered subsystems may be missing.[/yellow]"
        )

    # Surface specs whose analysis failed in the final round so users see
    # which specs need a re-run despite the loop terminating. Failed
    # analyses do not block convergence (see RoundResult.is_stable) — they
    # are reported here as a partial-success line instead.
    if rounds:
        final_analyses = getattr(rounds[-1], "analyses", []) or []
        failed = [a for a in final_analyses if getattr(a, "analysis_failed", False)]
        if failed:
            reason_counts: dict[str, int] = {}
            for a in failed:
                reason = getattr(a, "failed_analysis_reason", None) or "unknown"
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            reasons_str = ", ".join(
                f"{r}: {c}" for r, c in sorted(reason_counts.items())
            )
            summary_line += (
                f"\nPartial success: {len(failed)} spec(s) had analysis "
                f"failures (reasons: {reasons_str})"
            )

    render_block_header(title, color)
    console.print(summary_line)
    console.print("")
    console.print(CONVERGENCE_DISCLAIMER)
    console.print("")
    render_block_footer(color)


# Backwards-compatible alias: keep the historical name available for any
# external test code that imports it. The new canonical helper is
# ``_render_loop_result``.
_render_sync_results = _render_loop_result


def sync_command(
    max_rounds: int = 10,
    stable_rounds: int = 1,
    interactive: bool = False,
    show_diff: bool = False,
    once: bool = False,
    project_root: Optional[Path] = None,
    resume: bool = False,
    force: bool = False,
    confirm_cleanup: bool = False,
) -> None:
    """Run ``SyncLoop`` and render the final report.

    Args:
        max_rounds: Hard upper bound on rounds.
        stable_rounds: Consecutive zero-change rounds required for convergence.
        interactive: Forwarded to ``SyncLoop`` (gates high-impact deletions).
        show_diff: When True, print a per-round changes tree before the summary.
        once: Informational flag for the banner; the CLI layer already
            collapses ``max_rounds`` / ``stable_rounds`` to ``1`` in this case.
        project_root: Project root directory. Auto-detected if None.
        confirm_cleanup: When True, prompt before deleting each obsolete spec.
    """
    from ..engine import sync_checkpoint as _sync_checkpoint
    from ..engine.sync_loop import SyncLoop

    if project_root is None:
        from .run import get_project_root
        project_root = get_project_root()

    checkpoint = None
    if resume:
        checkpoint = _sync_checkpoint.load(project_root)
        if checkpoint is None:
            render_text(
                "No sync checkpoint found at se3/state/sync_checkpoint.json. "
                "Nothing to resume — start a fresh run with `se3 sync`.",
                title="SE3 Sync — Resume Error",
            )
            raise SystemExit(1)

    console = get_console()
    render_block_header("SE3 Sync", "blue")
    mode_label = "single-round" if once or max_rounds == 1 else "convergence loop"
    if resume:
        mode_label = f"{mode_label} (resumed)"
    console.print(
        f"Mode: [bold]{mode_label}[/bold]\n"
        f"Project: {project_root}\n"
        f"max_rounds={max_rounds}, stable_rounds={stable_rounds}, "
        f"interactive={interactive}, confirm_cleanup={confirm_cleanup}"
    )
    if checkpoint is not None:
        console.print(
            f"Resuming from round {checkpoint.round_index} "
            f"({len(checkpoint.in_sync_specs)} spec(s) already in sync)."
        )
    console.print("")
    render_block_footer("blue")

    logger.info(
        "se3 sync called: max_rounds=%s stable_rounds=%s interactive=%s "
        "once=%s project_root=%s",
        max_rounds, stable_rounds, interactive, once, project_root,
    )

    def progress_callback(phase: str, **kwargs) -> None:
        if phase == "round_start":
            ri = kwargs.get("round_index")
            console.print(f"[blue]→[/blue] Round {ri}: analyzing specs…")
        elif phase == "spec_analyzed":
            spec_name = kwargs.get("spec_name") or "?"
            analysis = kwargs.get("analysis")
            if analysis is None:
                return
            if getattr(analysis, "analysis_failed", False):
                reason = getattr(analysis, "failed_analysis_reason", None) or "unknown"
                console.print(
                    f"  [red]✗[/red] {spec_name}: analysis failed ({reason})"
                )
                return
            gaps = len(getattr(analysis, "gaps", []))
            exts = len(getattr(analysis, "extensions", []))
            confs = len(getattr(analysis, "conflicts", []))
            if getattr(analysis, "is_in_sync", False):
                console.print(f"  [green]✓[/green] {spec_name}: in sync")
            else:
                parts = []
                if gaps:
                    parts.append(f"{gaps} gap(s)")
                if exts:
                    parts.append(f"{exts} extension(s)")
                if confs:
                    parts.append(f"{confs} conflict(s)")
                console.print(
                    f"  [yellow]△[/yellow] {spec_name}: {', '.join(parts)}"
                )
        elif phase == "round_end":
            ri = kwargs.get("round_index")
            changes_by_spec = kwargs.get("changes_by_spec") or {}
            line = _format_round_line(ri, changes_by_spec)
            console.print(f"  {line}")
        elif phase == "converged":
            ri = kwargs.get("round_index")
            console.print(f"[green]✓ Converged at round {ri}[/green]")
        elif phase == "oscillation":
            ri = kwargs.get("round_index")
            console.print(
                f"[red]✗ Oscillation detected at round {ri}; aborting.[/red]"
            )
        elif phase == "max_rounds_exhausted":
            mr = kwargs.get("max_rounds")
            console.print(
                f"[yellow]⚠ Reached max_rounds={mr} without converging.[/yellow]"
            )
        elif phase == "paused":
            cp = kwargs.get("checkpoint_path")
            console.print(
                f"[yellow]⏸ Sync paused; checkpoint at {cp}. "
                f"Re-run with `se3 sync --resume` to continue.[/yellow]"
            )

    loop = SyncLoop(
        project_root=project_root,
        max_rounds=max_rounds,
        stable_rounds=stable_rounds,
        interactive=interactive,
        progress_callback=progress_callback,
        resume_from=checkpoint,
        force=force,
        confirm_cleanup=confirm_cleanup,
    )

    try:
        loop_result = loop.run()
    except KeyboardInterrupt:
        from ..engine import sync_checkpoint as _scp

        cp_exists = _scp.checkpoint_path(project_root).exists()
        if cp_exists:
            render_text(
                "Sync interrupted. Checkpoint preserved at "
                "se3/state/sync_checkpoint.json — re-run with `se3 sync --resume`.",
                title="SE3 Sync — Interrupted",
            )
        else:
            render_text(
                "Sync interrupted; no checkpoint was written, "
                "the next run starts fresh.",
                title="SE3 Sync — Interrupted",
            )
        raise SystemExit(130)

    console.print()
    _render_loop_result(loop_result, show_diff=show_diff)


def process_call_response(
    call_file: Path,
    project_root: Optional[Path] = None,
) -> None:
    """Process a ``sync_high_impact_deletion`` response file.

    The single-directional sync flow only emits this one call file type.
    Legacy ``sync_pending_decisions`` / ``sync_conflicts`` formats are no
    longer supported — those produce a clear error pointing the user at
    the new workflow.
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
        render_text(
            f"Response file not found: {response_path}",
            title="SE3 Sync Error",
        )
        return

    # Read just enough of the call file to validate its type up front, so
    # we can produce a friendly migration error before instantiating
    # SyncEngine (which would surface a less obvious error).
    try:
        call_data = json.loads(call_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        render_text(
            f"Could not parse call file: {e}",
            title="SE3 Sync Error",
        )
        return

    call_type = call_data.get("type")
    if call_type != "sync_high_impact_deletion":
        render_text(
            "Unsupported call file format. This file was generated by a "
            "previous version of se3 sync (type="
            f"{call_type!r}). Re-run 'se3 sync --interactive' to generate "
            "a fresh sync_high_impact_deletion call file.",
            title="SE3 Sync Error",
        )
        return

    engine = SyncEngine(project_root)
    try:
        result = engine.process_call_response(call_path)
    except ValueError as e:
        render_text(str(e), title="SE3 Sync Error")
        return

    specs_updated = result.get("specs_updated", 0)
    skipped = result.get("skipped", 0)
    render_text(
        f"Specs updated: {specs_updated}, Skipped: {skipped}",
        title="SE3 Sync — Call Response Processed",
    )


def validate_only_command(project_root: Optional[Path] = None) -> int:
    """Audit every ``se3/specs/*/spec.md`` against the v1 structural rules.

    Read-only: never invokes the LLM and never writes to disk. Prints a
    Rich table of per-spec results and returns the suggested process
    exit code (``0`` if every spec passes, ``1`` otherwise).
    """
    if project_root is None:
        from .run import get_project_root
        project_root = get_project_root()

    project_root = Path(project_root)
    specs_root = project_root / "se3" / "specs"
    console = get_console()

    render_block_header("SE3 Sync — Validate-Only", "blue")
    console.print(f"Project: {project_root}")
    console.print(f"Specs dir: {specs_root}")
    console.print("")
    render_block_footer("blue")

    if not specs_root.is_dir():
        render_text(
            f"No specs directory found at {specs_root}. Nothing to validate.",
            title="SE3 Sync — Validate-Only",
        )
        return 0

    results: list[tuple[str, bool, list[str]]] = []
    for entry in sorted(specs_root.iterdir()):
        if not entry.is_dir():
            continue
        # Convention: directories whose name starts with "_" or "." are
        # framework-internal (e.g. _changelog) and not specs.
        if entry.name.startswith("_") or entry.name.startswith("."):
            continue
        spec_path = entry / "spec.md"
        if not spec_path.exists():
            results.append((entry.name, False, ["spec.md missing"]))
            continue
        try:
            content = spec_path.read_text(encoding="utf-8")
        except OSError as exc:
            results.append((entry.name, False, [f"read error: {exc}"]))
            continue
        validation = validate_spec_structure(content, entry.name)
        results.append((entry.name, validation.passed, validation.errors))

    if not results:
        render_text(
            "No spec files found under se3/specs/.",
            title="SE3 Sync — Validate-Only",
        )
        return 0

    table = Table(title="Spec Validation Results", show_lines=False)
    table.add_column("Spec", style="bold")
    table.add_column("Status")
    table.add_column("Errors")
    failures = 0
    for name, passed, errors in results:
        if passed:
            table.add_row(name, "[green]PASS[/green]", "")
        else:
            failures += 1
            table.add_row(
                name,
                "[red]FAIL[/red]",
                "\n".join(errors) if errors else "(unknown)",
            )
    console.print(table)
    console.print("")

    if failures:
        render_text(
            f"{failures} spec(s) failed validation. See details above.",
            title="SE3 Sync — Validate-Only (FAIL)",
        )
        return 1

    render_text(
        f"All {len(results)} spec(s) passed v1 structural validation.",
        title="SE3 Sync — Validate-Only (OK)",
    )
    return 0
