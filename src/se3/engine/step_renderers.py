"""Step output rendering registry.

Provides a single entry point (render_step_output) for displaying step results.
Each step type can register a custom renderer; steps without one use the
default generic renderer.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional

from .display import get_console, render_design, render_full, render_markdown, render_proposal
from .models import Step, StepStatus, StepType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Human-readable display titles for every StepType
# ---------------------------------------------------------------------------

STEP_DISPLAY_TITLES: Dict[StepType, str] = {
    StepType.DISCOVERY: "Discovery",
    StepType.ANALYZE: "Analysis",
    StepType.PROJECT_SUMMARY: "Project Summary",
    StepType.READ_SPEC: "Spec Reading",
    StepType.PROPOSE: "Proposal",
    StepType.DESIGN: "Design",
    StepType.PLAN: "Planning",
    StepType.PLAN_TASKS: "Task Planning",
    StepType.CONFIRM: "Confirmation",
    StepType.IMPLEMENT: "Implementation",
    StepType.TEST: "Testing",
    StepType.VERIFY_SPEC: "Spec Verification",
    StepType.UPDATE_SPEC: "Spec Update",
    StepType.VERSION_ANALYZE: "Version Analysis",
    StepType.COMMIT: "Commit",
    StepType.SUMMARIZE: "Work Summary",
}


# ---------------------------------------------------------------------------
# Renderer registry
# ---------------------------------------------------------------------------

# StepType -> callable(step: Step) -> None
STEP_RENDERERS: Dict[StepType, Callable[[Step], None]] = {}


def register_renderer(step_type: StepType) -> Callable:
    """Decorator that registers a custom renderer for *step_type*."""

    def decorator(fn: Callable[[Step], None]) -> Callable[[Step], None]:
        STEP_RENDERERS[step_type] = fn
        return fn

    return decorator


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def render_step_output(step: Step) -> None:
    """Render a completed step's output.

    Looks up a custom renderer in STEP_RENDERERS; falls back to
    _default_render when none is registered.
    """
    title = STEP_DISPLAY_TITLES.get(step.step_type, step.step_type.value)

    renderer = STEP_RENDERERS.get(step.step_type)
    if renderer is not None:
        renderer(step)
    else:
        _default_render(step, title)


# ---------------------------------------------------------------------------
# Default (generic) renderer
# ---------------------------------------------------------------------------


def _format_value(value: Any) -> str:
    """Format a single output value for display."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _default_render(step: Step, title: str) -> None:
    """Generic renderer: iterates step.outputs and displays each key-value."""
    lines: list[str] = []

    # Show non-completed status
    if step.status not in (StepStatus.COMPLETED, StepStatus.RUNNING):
        lines.append(f"[bold]Status: {step.status.value}[/bold]")
        lines.append("")

    if step.outputs:
        for key, value in step.outputs.items():
            formatted = _format_value(value)
            # For long text values show a preview + length
            if isinstance(value, str) and len(value) > 300:
                preview = value[:200].replace("\n", " ")
                lines.append(f"  [bold]{key}:[/bold] {preview}… ({len(value)} chars)")
            else:
                lines.append(f"  [bold]{key}:[/bold] {formatted}")

    if step.error_message:
        if lines:
            lines.append("")
        lines.append(f"[bold red]Error:[/bold red] {step.error_message}")

    if lines:
        render_full("\n".join(lines), title=title)


def _render_remaining(step: Step, title: str, skip_keys: set[str]) -> None:
    """Render any step.outputs keys not in *skip_keys* using generic formatting."""
    outputs = step.outputs or {}
    remaining = {k: v for k, v in outputs.items() if k not in skip_keys and v}
    if not remaining:
        return
    lines: list[str] = []
    for key, value in remaining.items():
        formatted = _format_value(value)
        if isinstance(value, str) and len(value) > 300:
            preview = value[:200].replace("\n", " ")
            lines.append(f"  [bold]{key}:[/bold] {preview}… ({len(value)} chars)")
        else:
            lines.append(f"  [bold]{key}:[/bold] {formatted}")
    if lines:
        render_full("\n".join(lines), title=f"{title} — Additional Details")


# ---------------------------------------------------------------------------
# Custom renderers
# ---------------------------------------------------------------------------


@register_renderer(StepType.VERSION_ANALYZE)
def _render_version_analyze(step: Step) -> None:
    outputs = step.outputs or {}
    va_lines = [
        f"[bold]Current Version:[/bold] {outputs.get('current_version', 'N/A')}",
        f"[bold]Suggested Version:[/bold] {outputs.get('suggested_version', 'N/A')}",
        f"[bold]Bump Type:[/bold] {outputs.get('bump_type', 'N/A')}",
        f"[bold]Confidence:[/bold] {outputs.get('confidence', 'N/A')}",
        "",
        "[bold]Reasoning:[/bold]",
        outputs.get("reasoning", ""),
    ]
    render_full("\n".join(va_lines), title="Version Analysis")


@register_renderer(StepType.SUMMARIZE)
def _render_summarize(step: Step) -> None:
    summary = (step.outputs or {}).get("summary", "")
    if summary:
        render_markdown(summary, title="Work Summary")


@register_renderer(StepType.TEST)
def _render_test(step: Step) -> None:
    outputs = step.outputs or {}
    test_results = outputs.get("test_results")
    if not test_results or not isinstance(test_results, dict):
        _default_render(step, "Testing")
        return

    overall_passed = test_results.get("overall_passed", test_results.get("passed", False))
    status = "[bold green]PASSED[/bold green]" if overall_passed else "[bold red]FAILED[/bold red]"

    lines = [f"[bold]Status:[/bold] {status}"]

    # Count passed/failed phases
    phase_results = test_results.get("phases", [])
    if phase_results:
        passed_count = sum(1 for p in phase_results if p.get("passed", False))
        failed_count = len(phase_results) - passed_count
        lines.append(f"[bold]Phases:[/bold] {passed_count} passed, {failed_count} failed")
        for phase in phase_results:
            name = phase.get("name", "?")
            p = phase.get("passed", False)
            indicator = "[green]✓[/green]" if p else "[red]✗[/red]"
            lines.append(f"  {indicator} {name}")

    command = test_results.get("command", "")
    if command:
        lines.append(f"[bold]Command:[/bold] {command}")

    render_full("\n".join(lines), title="Testing")


@register_renderer(StepType.PROPOSE)
def _render_propose(step: Step) -> None:
    outputs = step.outputs or {}
    proposal_key: Optional[str] = None
    for key in ("proposal", "proposal_data"):
        if key in outputs and isinstance(outputs[key], dict):
            proposal_key = key
            render_proposal(outputs[key])
            break

    if proposal_key is None:
        _default_render(step, "Proposal")
        return

    # Render remaining outputs that aren't the proposal dict or its extracted sub-keys
    _PROPOSE_DEFERRED = {proposal_key, "summary", "files_to_modify", "files_to_create"}
    _render_remaining(step, "Proposal", _PROPOSE_DEFERRED)


@register_renderer(StepType.DESIGN)
def _render_design(step: Step) -> None:
    outputs = step.outputs or {}
    design_key: Optional[str] = None
    for key in ("design", "design_doc", "design_document"):
        if key in outputs and isinstance(outputs[key], dict):
            design_key = key
            render_design(outputs[key])
            break

    if design_key is None:
        _default_render(step, "Design")
        return

    # Render remaining outputs that aren't the design dict or its extracted sub-keys
    _DESIGN_DEFERRED = {design_key, "decisions", "components", "implementation_plan"}
    _render_remaining(step, "Design", _DESIGN_DEFERRED)


@register_renderer(StepType.PLAN)
def _render_plan(step: Step) -> None:
    outputs = step.outputs or {}

    # The PLAN step outputs: {plan: {proposal: {...}, design: {...}}, task_groups: [...], ...}
    plan_data = outputs.get("plan", {})
    proposal = plan_data.get("proposal") if isinstance(plan_data, dict) else None
    design = plan_data.get("design") if isinstance(plan_data, dict) else None
    task_groups = outputs.get("task_groups", [])

    rendered_any = False

    # Section 1: Proposal
    if isinstance(proposal, dict):
        render_proposal(proposal)
        rendered_any = True
    elif isinstance(proposal, str) and proposal:
        render_full(f"[bold]Proposal:[/bold] {proposal}", title="Planning — Proposal")
        rendered_any = True

    # Section 2: Design
    if isinstance(design, dict):
        render_design(design)
        rendered_any = True
    elif isinstance(design, str) and design:
        render_full(f"[bold]Design:[/bold] {design}", title="Planning — Design")
        rendered_any = True

    # Section 3: Task Groups
    if task_groups and isinstance(task_groups, list):
        lines: list[str] = []
        for group in task_groups:
            if not isinstance(group, dict):
                continue
            gid = group.get("group_id", "?")
            name = group.get("name", "")
            tasks = group.get("tasks", [])
            task_count = len(tasks) if isinstance(tasks, list) else 0
            total_loc = sum(
                t.get("estimated_loc", 0) for t in tasks if isinstance(t, dict)
            ) if isinstance(tasks, list) else 0
            deps = group.get("depends_on", [])
            dep_str = ", ".join(str(d) for d in deps) if deps else "none"
            lines.append(
                f"  [bold]{gid}[/bold] {name}  "
                f"— {task_count} tasks, ~{total_loc} LOC, depends: {dep_str}"
            )
        if lines:
            render_full("\n".join(lines), title="Planning — Task Groups")
            rendered_any = True

    if not rendered_any:
        _default_render(step, "Planning")
        return

    # Render any remaining keys not already covered
    _render_remaining(step, "Planning", {"plan", "task_groups", "total_complexity", "estimated_effort"})


def _group_files_by_directory(files: list[str]) -> dict[str, list[str]]:
    """Group file paths by their top-level directory.

    Returns an OrderedDict mapping directory prefixes (e.g. ``src/``, ``tests/``)
    to sorted lists of file paths.  Files without a directory component are
    placed under ``./``, which is sorted last.
    """
    from collections import OrderedDict

    groups: dict[str, list[str]] = {}
    for filepath in files:
        parts = filepath.replace("\\", "/").split("/")
        if len(parts) > 1:
            dir_prefix = parts[0] + "/"
        else:
            dir_prefix = "./"
        groups.setdefault(dir_prefix, []).append(filepath)

    sorted_groups: dict[str, list[str]] = OrderedDict()
    for key in sorted(groups.keys(), key=lambda k: (k == "./", k)):
        sorted_groups[key] = sorted(groups[key])
    return sorted_groups


@register_renderer(StepType.IMPLEMENT)
def _render_implement(step: Step) -> None:
    outputs = step.outputs or {}

    completion_status = outputs.get("completion_status", "unknown")
    files_changed = outputs.get("files_changed", [])
    tests_added = outputs.get("tests_added", [])
    implemented_groups = outputs.get("implemented_groups", [])
    summary = outputs.get("summary", "")
    incomplete_tasks = outputs.get("incomplete_tasks", [])
    restricted_applied = outputs.get("restricted_edits_applied", [])
    restricted_failed = outputs.get("restricted_edits_failed", [])

    lines: list[str] = []

    # ── Top status summary bar ──────────────────────────────────────
    status_icons = {
        "complete": "[bold green]✓[/bold green]",
        "partial": "[bold yellow]◐[/bold yellow]",
        "failed": "[bold red]✗[/bold red]",
    }
    status_labels = {
        "complete": "[green]Complete[/green]",
        "partial": "[yellow]Partial[/yellow]",
        "failed": "[red]Failed[/red]",
    }
    icon = status_icons.get(completion_status, "●")
    label = status_labels.get(completion_status, completion_status)

    groups_count = len(implemented_groups) if implemented_groups else 0
    files_count = len(files_changed) if files_changed else 0
    tests_count = len(tests_added) if tests_added else 0

    stats = [f"{icon} {label}"]
    if groups_count:
        stats.append(f"[bold]{groups_count}[/bold] groups")
    stats.append(f"[bold]{files_count}[/bold] files")
    if tests_count:
        stats.append(f"[bold]{tests_count}[/bold] tests")
    lines.append("  │  ".join(stats))

    # ── Summary ─────────────────────────────────────────────────────
    if summary:
        lines.append("")
        lines.append("[dim]" + "─" * 50 + "[/dim]")
        lines.append("")
        lines.append("[bold cyan]Summary[/bold cyan]")
        parts = [s.strip() for s in summary.split(";") if s.strip()]
        if len(parts) == 1:
            lines.append(f"  {parts[0]}")
        else:
            for i, part in enumerate(parts, 1):
                gid = f"G{i}" if implemented_groups else str(i)
                lines.append(f"  [dim]{gid}.[/dim] {part}")

    # ── Files Changed ───────────────────────────────────────────────
    if files_changed:
        lines.append("")
        lines.append("[dim]" + "─" * 50 + "[/dim]")
        lines.append("")
        lines.append(f"[bold yellow]Files Changed[/bold yellow]  [dim]({files_count})[/dim]")
        grouped = _group_files_by_directory(files_changed)
        for dir_prefix, filenames in grouped.items():
            lines.append(f"  [bold]{dir_prefix}[/bold] [dim]({len(filenames)})[/dim]")
            for fname in filenames:
                lines.append(f"    {fname}")

    # ── Tests Added ─────────────────────────────────────────────────
    if tests_added:
        lines.append("")
        lines.append("[dim]" + "─" * 50 + "[/dim]")
        lines.append("")
        lines.append(f"[bold green]Tests Added[/bold green]  [dim]({tests_count})[/dim]")
        for t in tests_added:
            lines.append(f"  [green]+[/green] {t}")

    # ── Incomplete Tasks ────────────────────────────────────────────
    if incomplete_tasks:
        lines.append("")
        lines.append("[dim]" + "─" * 50 + "[/dim]")
        lines.append("")
        lines.append(f"[bold red]Incomplete Tasks[/bold red]  [dim]({len(incomplete_tasks)})[/dim]")
        for task in incomplete_tasks:
            if isinstance(task, dict):
                tid = task.get("task_id", task.get("id", "?"))
                reason = task.get("reason", task.get("error", ""))
                lines.append(f"  [red]✗[/red] [bold]{tid}[/bold]{f': {reason}' if reason else ''}")
            else:
                lines.append(f"  [red]✗[/red] {task}")

    # ── Restricted Edits ────────────────────────────────────────────
    if restricted_applied or restricted_failed:
        lines.append("")
        if restricted_applied:
            lines.append(f"[dim]Restricted edits applied: {len(restricted_applied)}[/dim]")
        if restricted_failed:
            lines.append(f"[bold red]Restricted edits failed: {len(restricted_failed)}[/bold red]")
            for edit in restricted_failed:
                if isinstance(edit, dict):
                    lines.append(f"  • {edit.get('file', edit.get('path', str(edit)))}")
                else:
                    lines.append(f"  • {edit}")

    # ── Error ───────────────────────────────────────────────────────
    if step.error_message:
        lines.append("")
        lines.append(f"[bold red]Error:[/bold red] {step.error_message}")

    render_full("\n".join(lines), title="Implementation")


@register_renderer(StepType.ANALYZE)
def _render_analyze(step: Step) -> None:
    outputs = step.outputs or {}

    task_type = outputs.get("task_type", "N/A")
    complexity = outputs.get("complexity", "N/A")
    scope = outputs.get("scope", "N/A")

    lines: list[str] = []

    # ── Top status bar ─────────────────────────────────────────────
    lines.append(f"[bold]{task_type}[/bold]  │  {complexity}  │  {scope}")

    # ── Reasoning ──────────────────────────────────────────────────
    reasoning = outputs.get("reasoning", "")
    if reasoning:
        lines.append("")
        lines.append("[dim]" + "─" * 50 + "[/dim]")
        lines.append("")
        lines.append("[bold cyan]Reasoning[/bold cyan]")
        lines.append(f"  {reasoning}")

    # ── Relevant Specs ─────────────────────────────────────────────
    relevant_specs = outputs.get("relevant_specs", [])
    if relevant_specs:
        lines.append("")
        lines.append("[dim]" + "─" * 50 + "[/dim]")
        lines.append("")
        lines.append("[bold yellow]Relevant Specs[/bold yellow]")
        for spec in relevant_specs:
            if isinstance(spec, dict):
                name = spec.get("name", spec.get("spec_name", str(spec)))
            else:
                name = str(spec)
            lines.append(f"  • {name}")

    # ── Error ──────────────────────────────────────────────────────
    if step.error_message:
        lines.append("")
        lines.append(f"[bold red]Error:[/bold red] {step.error_message}")

    render_full("\n".join(lines), title="Analysis")


@register_renderer(StepType.VERIFY_SPEC)
def _render_verify_spec(step: Step) -> None:
    outputs = step.outputs or {}

    lines: list[str] = []

    # ── Verified status ────────────────────────────────────────────
    verified = outputs.get("verified", outputs.get("fix_needed") is not None and not outputs.get("fix_needed"))
    if verified:
        lines.append("[bold green]✓ PASSED[/bold green]")
    else:
        lines.append("[bold red]✗ FAILED[/bold red]")

    # ── Summary ────────────────────────────────────────────────────
    verification_result = outputs.get("verification_result", {})
    summary = outputs.get("summary", "") or (verification_result.get("summary", "") if isinstance(verification_result, dict) else "")
    if summary:
        lines.append("")
        lines.append("[dim]" + "─" * 50 + "[/dim]")
        lines.append("")
        lines.append(f"  {summary}")

    # ── Issues by scope and priority ─────────────────────────────────
    issues = outputs.get("issues", [])
    if issues and isinstance(issues, list):
        # Group by scope first, then by priority within each scope
        scope_groups: Dict[str, list] = {"in_scope": [], "out_of_scope": []}
        for issue in issues:
            if isinstance(issue, dict):
                scope = issue.get("scope", "in_scope")
                scope_groups.setdefault(scope, []).append(issue)
            else:
                scope_groups["in_scope"].append({"message": str(issue), "priority": "medium"})

        priority_styles = {
            "critical": ("[bold red]critical[/bold red]", "[red]"),
            "high": ("[bold red]high[/bold red]", "[red]"),
            "medium": ("[bold yellow]medium[/bold yellow]", "[yellow]"),
            "low": ("[dim]low[/dim]", "[dim]"),
        }

        for scope_label, scope_key in [("In-scope", "in_scope"), ("Out-of-scope", "out_of_scope")]:
            group = scope_groups.get(scope_key, [])
            if not group:
                continue
            lines.append("")
            lines.append("[dim]" + "─" * 50 + "[/dim]")
            lines.append("")
            scope_style = "[bold red]" if scope_key == "in_scope" else "[dim]"
            lines.append(f"{scope_style}{scope_label}[/{scope_style[1:-1]}]  [dim]({len(group)})[/dim]")
            for issue in group:
                msg = issue.get("message", "") if isinstance(issue, dict) else str(issue)
                prio = issue.get("priority", "medium").lower() if isinstance(issue, dict) else "medium"
                _label, color = priority_styles.get(prio, ("[dim]medium[/dim]", "[dim]"))
                lines.append(f"  {color}•[/{color[1:-1]}] [{prio}] {msg}")
                suggestion = issue.get("suggestion", "") if isinstance(issue, dict) else ""
                if suggestion:
                    lines.append(f"    [dim]→ {suggestion}[/dim]")

    # ── Recommendations ────────────────────────────────────────────
    recommendations = outputs.get("recommendations", []) or (verification_result.get("recommendations", []) if isinstance(verification_result, dict) else [])
    if recommendations and isinstance(recommendations, list):
        lines.append("")
        lines.append("[dim]" + "─" * 50 + "[/dim]")
        lines.append("")
        lines.append("[bold cyan]Recommendations[/bold cyan]")
        for rec in recommendations:
            lines.append(f"  • {rec}")

    # ── Error ──────────────────────────────────────────────────────
    if step.error_message:
        lines.append("")
        lines.append(f"[bold red]Error:[/bold red] {step.error_message}")

    render_full("\n".join(lines), title="Spec Verification")


@register_renderer(StepType.UPDATE_SPEC)
def _render_update_spec(step: Step) -> None:
    outputs = step.outputs or {}

    specs_updated = outputs.get("updated_specs", outputs.get("specs_updated", []))
    new_capabilities = outputs.get("new_capabilities", [])

    if not specs_updated and not new_capabilities:
        render_full("[dim]No spec updates needed[/dim]", title="Spec Update")
        return

    lines: list[str] = []

    # ── Updated specs ──────────────────────────────────────────────
    if specs_updated and isinstance(specs_updated, list):
        for spec in specs_updated:
            if isinstance(spec, dict):
                name = spec.get("spec_name", spec.get("name", "unknown"))
                desc = spec.get("change_description", spec.get("description", ""))
                lines.append(f"  [green]✓[/green] [bold]{name}[/bold]: {desc}")
            else:
                lines.append(f"  [green]✓[/green] {spec}")

    # ── New capabilities ───────────────────────────────────────────
    if new_capabilities and isinstance(new_capabilities, list):
        if lines:
            lines.append("")
            lines.append("[dim]" + "─" * 50 + "[/dim]")
            lines.append("")
        lines.append("[bold cyan]New Capabilities[/bold cyan]")
        for cap in new_capabilities:
            lines.append(f"  • {cap}")

    # ── Error ──────────────────────────────────────────────────────
    if step.error_message:
        lines.append("")
        lines.append(f"[bold red]Error:[/bold red] {step.error_message}")

    render_full("\n".join(lines), title="Spec Update")


@register_renderer(StepType.COMMIT)
def _render_commit(step: Step) -> None:
    outputs = step.outputs or {}

    committed = outputs.get("committed", False)

    if not committed:
        render_full("[dim]No changes to commit[/dim]", title="Commit")
        return

    lines: list[str] = []

    # ── Top line: hash + version ───────────────────────────────────
    commit_hash = outputs.get("commit_hash", "N/A")
    short_hash = commit_hash[:7] if isinstance(commit_hash, str) and len(commit_hash) > 7 else commit_hash
    header = f"[bold]{short_hash}[/bold]"

    version_bumped = outputs.get("version_bumped", False)
    version = outputs.get("version", "")
    if version_bumped and version:
        header += f"  │  [bold cyan]v{version}[/bold cyan]"

    lines.append(header)

    # ── Commit message ─────────────────────────────────────────────
    commit_message = outputs.get("commit_message", "")
    if commit_message:
        lines.append("")
        lines.append("[dim]" + "─" * 50 + "[/dim]")
        lines.append("")
        lines.append(f"  {commit_message}")

    # ── Error ──────────────────────────────────────────────────────
    if step.error_message:
        lines.append("")
        lines.append(f"[bold red]Error:[/bold red] {step.error_message}")

    render_full("\n".join(lines), title="Commit")
