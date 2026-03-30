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


@register_renderer(StepType.ANALYZE)
def _render_analyze(step: Step) -> None:
    outputs = step.outputs or {}

    # Check for structured analysis result
    for key in ("analysis", "analysis_result"):
        if key in outputs and isinstance(outputs[key], dict):
            value = outputs[key]
            if any(k in value for k in ("summary", "findings", "insights", "recommendations")):
                _render_analysis_dict(value)
                return

    # Otherwise use default rendering
    _default_render(step, "Analysis")


def _render_analysis_dict(analysis: Dict[str, Any]) -> None:
    """Render a structured analysis dict (migrated from output.display_analysis)."""
    lines: list[str] = []

    summary = analysis.get("summary", "")
    if summary:
        lines.append("[bold cyan]Analysis Summary[/bold cyan]")
        lines.append(summary)
        lines.append("")

    findings = analysis.get("findings", analysis.get("insights", []))
    if findings:
        lines.append("[bold yellow]Findings[/bold yellow]")
        for finding in findings:
            if isinstance(finding, dict):
                ftitle = finding.get("title", finding.get("name", ""))
                desc = finding.get("description", finding.get("detail", ""))
                if ftitle:
                    lines.append(f"\n[bold]{ftitle}[/bold]")
                if desc:
                    lines.append(desc)
            else:
                lines.append(f"  • {finding}")
        lines.append("")

    recommendations = analysis.get("recommendations", [])
    if recommendations:
        lines.append("[bold green]Recommendations[/bold green]")
        for rec in recommendations:
            if isinstance(rec, dict):
                action = rec.get("action", rec.get("recommendation", ""))
                priority = rec.get("priority", "")
                if priority:
                    lines.append(f"\n• [{priority}] {action}")
                else:
                    lines.append(f"• {action}")
            else:
                lines.append(f"  • {rec}")
        lines.append("")

    for key, value in analysis.items():
        if key not in ("summary", "findings", "insights", "recommendations"):
            if value:
                lines.append(f"[bold]{key.replace('_', ' ').title()}[/bold]")
                if isinstance(value, list):
                    for item in value:
                        lines.append(f"  • {item}")
                elif isinstance(value, dict):
                    for k, v in value.items():
                        lines.append(f"  [bold]{k}:[/bold] {v}")
                else:
                    lines.append(str(value))
                lines.append("")

    if lines:
        render_full("\n".join(lines), title="Analysis Results")
