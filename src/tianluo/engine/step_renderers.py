"""Step output rendering registry.

Provides a single entry point (render_step_output) for displaying step results.
Each step type can register a custom renderer; steps without one use the
default generic renderer.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional

from ..i18n import t
from .display import (
    get_console,
    render_design,
    render_full,
    render_markdown,
    render_proposal,
    render_usage_block,
)
from .models import Step, StepStatus, StepType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Human-readable display titles for every StepType
# ---------------------------------------------------------------------------

# Per-StepType i18n key for the panel/report title. Resolved lazily through
# ``t()`` at render time (not at import) so the active UI language wins even
# though this map is built once at module load.
STEP_TITLE_KEYS: Dict[StepType, str] = {
    StepType.DISCOVERY: "cli.steprender.title.discovery",
    StepType.ANALYZE: "cli.steprender.title.analyze",
    StepType.INVESTIGATE: "cli.steprender.title.investigate",
    StepType.PROJECT_SUMMARY: "cli.steprender.title.project_summary",
    StepType.PROPOSE: "cli.steprender.title.propose",
    StepType.DESIGN: "cli.steprender.title.design",
    StepType.PLAN: "cli.steprender.title.plan",
    StepType.PLAN_TASKS: "cli.steprender.title.plan_tasks",
    StepType.CONFIRM: "cli.steprender.title.confirm",
    StepType.IMPLEMENT: "cli.steprender.title.implement",
    StepType.TEST: "cli.steprender.title.test",
    StepType.E2E: "cli.steprender.title.e2e",
    StepType.SELF_CHECK: "cli.steprender.title.self_check",
    StepType.VERIFY_SPEC: "cli.steprender.title.verify_spec",
    StepType.UPDATE_SPEC: "cli.steprender.title.update_spec",
    StepType.SPEC_GATE: "cli.steprender.title.spec_gate",
    StepType.VERSION_ANALYZE: "cli.steprender.title.version_analyze",
    StepType.COMMIT: "cli.steprender.title.commit",
    StepType.SUMMARIZE: "cli.steprender.title.summarize",
    StepType.MERGE_INTEGRATE: "cli.steprender.title.merge_integrate",
    StepType.VERSION_RECONCILE: "cli.steprender.title.version_reconcile",
}


def step_display_title(step_type: StepType) -> str:
    """Localized display title for *step_type*, falling back to its raw value.

    Public so the ``luo run`` console (which prints its own per-step Rule header,
    not a renderer Panel) resolves step titles from the same map — one language
    for every user-visible mention of a step.
    """
    key = STEP_TITLE_KEYS.get(step_type)
    return t(key) if key else step_type.value


# Internal alias kept for the renderers below, which all read as `_title_for`.
_title_for = step_display_title


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
    title = _title_for(step.step_type)

    renderer = STEP_RENDERERS.get(step.step_type)
    if renderer is not None:
        renderer(step)
    else:
        _default_render(step, title)

    # Append the step's token-usage summary block after its own report. Steps
    # with no LLM consumption (no token_usage in outputs) are byte-identical to
    # before. Note that CliSink skips render_step_output for confirm/discovery/
    # plan (their report is owned by the orchestrator's interactive paths), but
    # it calls render_step_usage directly for those step types so token-heavy
    # steps like plan/discovery still show their per-step usage on the CLI.
    render_step_usage(step)


def render_step_usage(step: Step) -> None:
    """Render the per-step token-usage block when the step consumed tokens.

    G2's ``state_machine.run_step`` writes a non-empty ``token_usage`` dict into
    ``step.outputs`` whenever the step made at least one LLM call — **for both
    terminal and non-terminal runs** (COMPLETED/PARTIAL/FAILED as well as
    PAUSED/REVISION_NEEDED/RETRYING). Absent or empty usage renders nothing
    (``render_usage_block`` also guards is_empty), keeping non-LLM steps
    byte-identical.

    This function reads **only** ``outputs.token_usage``, never the internal
    ``carried_token_usage`` field, so both CLI and WebUI report cards share a
    single, consistent display source. The engine ensures that
    ``carried_token_usage`` is never needed for rendering: non-terminal runs
    publish the combined (carried + current) total as ``token_usage`` as well.

    Exposed publicly so ``CliSink`` can render the usage block for the
    interactive/special step types (confirm/discovery/plan) whose full report
    rendering it skips — keeping CLI per-step usage symmetric across all steps.
    """
    usage = (step.outputs or {}).get("token_usage")
    if not usage:
        return
    render_usage_block(usage, title=t("cli.steprender.usage.title"))


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
        lines.append(f"[bold]{t('cli.steprender.status', status=step.status.value)}[/bold]")
        lines.append("")

    if step.outputs:
        for key, value in step.outputs.items():
            formatted = _format_value(value)
            # For long text values show a preview + length
            if isinstance(value, str) and len(value) > 300:
                preview = value[:200].replace("\n", " ")
                lines.append(
                    f"  [bold]{key}:[/bold] {preview}… "
                    f"{t('cli.steprender.chars_suffix', count=len(value))}"
                )
            else:
                lines.append(f"  [bold]{key}:[/bold] {formatted}")

    if step.error_message:
        if lines:
            lines.append("")
        lines.append(f"[bold red]{t('cli.steprender.error')}[/bold red] {step.error_message}")

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
            lines.append(
                f"  [bold]{key}:[/bold] {preview}… "
                f"{t('cli.steprender.chars_suffix', count=len(value))}"
            )
        else:
            lines.append(f"  [bold]{key}:[/bold] {formatted}")
    if lines:
        render_full("\n".join(lines), title=t("cli.steprender.remaining.suffix", title=title))


# ---------------------------------------------------------------------------
# Custom renderers
# ---------------------------------------------------------------------------


@register_renderer(StepType.VERSION_ANALYZE)
def _render_version_analyze(step: Step) -> None:
    outputs = step.outputs or {}

    current_version = outputs.get("current_version", "N/A")
    suggested_version = outputs.get("suggested_version", "N/A")
    bump_type = outputs.get("bump_type", "N/A")
    confidence = outputs.get("confidence", "N/A")
    reasoning = outputs.get("reasoning", "")

    lines: list[str] = []

    # ── Top line: current → suggested (authoritative) ─────────────
    lines.append(
        f"[bold]{current_version}[/bold] → [bold cyan]{suggested_version}[/bold cyan]"
    )

    # ── Sub-line: bump_type + confidence (auxiliary) ──────────────
    lines.append(t("cli.steprender.version_analyze.subline", bump_type=bump_type, confidence=confidence))

    # ── Reasoning ──────────────────────────────────────────────────
    if reasoning:
        lines.append("")
        lines.append("[dim]" + "─" * 50 + "[/dim]")
        lines.append("")
        lines.append(f"[bold cyan]{t('cli.steprender.section.reasoning')}[/bold cyan]")
        lines.append(f"  {reasoning}")

    # ── Error ──────────────────────────────────────────────────────
    if step.error_message:
        lines.append("")
        lines.append(f"[bold red]{t('cli.steprender.error')}[/bold red] {step.error_message}")

    render_full("\n".join(lines), title=t("cli.steprender.title.version_analyze"))


@register_renderer(StepType.MERGE_INTEGRATE)
def _render_merge_integrate(step: Step) -> None:
    """Render the merge_integrate step: which branch landed on master."""
    outputs = step.outputs or {}
    result = outputs.get("merge_result") or {}
    merged = outputs.get("merged_branches") or result.get("merged_branches") or []

    lines: list[str] = []
    if merged:
        joined = ", ".join(str(b) for b in merged)
        lines.append(t("cli.steprender.merge.merged_into", joined=joined))
    else:
        lines.append(t("cli.steprender.merge.none"))

    if result.get("pending_human"):
        lines.append("")
        lines.append(t("cli.steprender.merge.escalated"))

    if step.error_message:
        lines.append("")
        lines.append(f"[bold red]{t('cli.steprender.error')}[/bold red] {step.error_message}")

    render_full("\n".join(lines), title=t("cli.steprender.title.merge_integrate"))


@register_renderer(StepType.VERSION_RECONCILE)
def _render_version_reconcile(step: Step) -> None:
    """Render the version_reconcile step: the final version derived at merge."""
    outputs = step.outputs or {}
    result = outputs.get("reconcile_result") or {}
    base = outputs.get("base_version") or result.get("base_version") or "N/A"
    final = outputs.get("final_version") or result.get("final_version")
    channel = outputs.get("channel") or result.get("channel") or "N/A"

    lines: list[str] = []
    if result.get("already_reconciled") and not final:
        # Nothing outstanding to reconcile — a clean no-op, not a fault.
        lines.append(t("cli.steprender.reconcile.already", base=base))
    else:
        lines.append(
            f"[bold]{base}[/bold] → [bold cyan]{final or 'N/A'}[/bold cyan]"
        )
    lines.append(t("cli.steprender.reconcile.channel", channel=channel))

    commit = result.get("reconcile_commit")
    if commit:
        lines.append(t("cli.steprender.reconcile.commit", commit=str(commit)[:12]))

    if step.error_message:
        lines.append("")
        lines.append(f"[bold red]{t('cli.steprender.error')}[/bold red] {step.error_message}")

    render_full("\n".join(lines), title=t("cli.steprender.title.version_reconcile"))


@register_renderer(StepType.SUMMARIZE)
def _render_summarize(step: Step) -> None:
    summary = (step.outputs or {}).get("summary", "")
    if summary:
        render_markdown(summary, title=t("cli.steprender.title.summarize"))


def _build_test_summary_lines(test_results: Dict[str, Any]) -> list[str]:
    """Build the summary lines for a ``test_results`` dict (no raw stdout/stderr).

    Shared by the ``test`` and ``spec_gate`` renderers so both present the same
    summary-only view (overall status, phase pass/fail counts and list, command)
    instead of dumping the raw pytest output.
    """
    overall_passed = test_results.get("overall_passed", test_results.get("passed", False))
    status = t("cli.steprender.test.passed") if overall_passed else t("cli.steprender.test.failed")

    lines = [t("cli.steprender.test.status", status=status)]

    # Count passed/failed phases
    phase_results = test_results.get("phases", [])
    if phase_results:
        passed_count = sum(1 for p in phase_results if p.get("passed", False))
        failed_count = len(phase_results) - passed_count
        lines.append(t("cli.steprender.test.phases", passed=passed_count, failed=failed_count))
        for phase in phase_results:
            name = phase.get("name", "?")
            p = phase.get("passed", False)
            indicator = "[green]✓[/green]" if p else "[red]✗[/red]"
            lines.append(f"  {indicator} {name}")

    command = test_results.get("command", "")
    if command:
        lines.append(t("cli.steprender.test.command", command=command))

    return lines


@register_renderer(StepType.TEST)
def _render_test(step: Step) -> None:
    outputs = step.outputs or {}
    test_results = outputs.get("test_results")
    if not test_results or not isinstance(test_results, dict):
        _default_render(step, t("cli.steprender.title.test"))
        return

    lines = _build_test_summary_lines(test_results)
    # The e2e enable hint rides on the test step because that is where e2e would
    # run; it is advice only — nothing here or downstream touches tianluo.yaml.
    suggestion = outputs.get("e2e_suggestion")
    if suggestion:
        lines.append("")
        lines.append(str(suggestion))
    render_full("\n".join(lines), title=t("cli.steprender.title.test"))


# How much of one failed scenario is shown. e2e failures carry container logs and
# every evaluated assertion; dumping them here would bury the one line that says
# what broke. The full record stays in step.outputs (WebUI / history) and the
# verbatim detail reaches the implementing agent through fix_instructions.
E2E_MAX_FAILED_SCENARIOS = 5
E2E_MAX_ASSERTIONS_PER_SCENARIO = 3
E2E_MAX_VALUE_CHARS = 160


def _e2e_clip(value: Any) -> str:
    """One-line, length-capped rendering of an expected/actual value."""
    text = str(value if value is not None else "").replace("\n", " ").strip()
    if not text:
        return "-"
    if len(text) > E2E_MAX_VALUE_CHARS:
        return text[:E2E_MAX_VALUE_CHARS] + "…"
    return text


@register_renderer(StepType.E2E)
def _render_e2e(step: Step) -> None:
    """Render the e2e step as a scenario-level summary.

    Mirrors ``_render_test``'s summary-only presentation and falls back to the
    generic renderer whenever ``e2e_results`` is absent or not a mapping — the
    step can fail before it ever produces structured results (an unusable
    container runtime), and a renderer that assumed the happy shape would replace
    that diagnosis with a traceback.
    """
    outputs = step.outputs or {}
    results = outputs.get("e2e_results")
    if not isinstance(results, dict) or not results:
        _default_render(step, t("cli.steprender.title.e2e"))
        return

    lines: list[str] = []

    if results.get("skipped"):
        lines.append(t("cli.steprender.e2e.skipped"))
        render_full("\n".join(lines), title=t("cli.steprender.title.e2e"))
        return

    environment_error = results.get("environment_error") or outputs.get(
        "environment_error"
    )
    config_error = results.get("config_error")
    if environment_error or config_error:
        if config_error:
            lines.append(t("cli.steprender.e2e.config_error", message=config_error))
        else:
            lines.append(
                t("cli.steprender.e2e.environment_error", message=environment_error)
            )
        remediation = results.get("remediation") or outputs.get("e2e_remediation")
        if remediation:
            lines.append("")
            lines.append(str(remediation))
        render_full("\n".join(lines), title=t("cli.steprender.title.e2e"))
        return

    total = results.get("total", 0) or 0
    failed_count = results.get("failed", 0) or 0
    passed_count = results.get("passed", 0) or 0

    # A critical scenario that produced no result blocks the verdict without
    # appearing in `failed`, so the headline has to account for it — otherwise the
    # panel would read "passed" above a step the state machine sent to the fix
    # loop.
    unverified = results.get("critical_unverified")
    unverified = [name for name in unverified if isinstance(name, str)] if isinstance(
        unverified, list
    ) else []

    status = (
        t("cli.steprender.e2e.failed")
        if failed_count or unverified
        else t("cli.steprender.e2e.passed")
    )
    lines.append(t("cli.steprender.e2e.status", status=status))
    if unverified:
        lines.append(
            t(
                "cli.steprender.e2e.critical_unverified",
                scenarios=", ".join(unverified),
            )
        )
    lines.append(
        t(
            "cli.steprender.e2e.counts",
            passed=passed_count,
            failed=failed_count,
            total=total,
        )
    )

    runtime = results.get("runtime") or ""
    if runtime:
        lines.append(t("cli.steprender.e2e.runtime", runtime=runtime))

    # Both halves of the wall clock, kept apart: a slow step is diagnosed very
    # differently depending on whether the time went into rebuilding the
    # environment or into running the scenarios.
    environment_duration = results.get("environment_duration")
    if isinstance(environment_duration, (int, float)) and not isinstance(
        environment_duration, bool
    ):
        lines.append(
            t(
                "cli.steprender.e2e.environment_duration",
                seconds=f"{environment_duration:.1f}",
            )
        )

    duration = results.get("duration")
    if isinstance(duration, (int, float)):
        lines.append(t("cli.steprender.e2e.duration", seconds=f"{duration:.1f}"))

    if not total:
        lines.append(t("cli.steprender.e2e.no_scenarios"))

    scenarios = results.get("scenarios")
    scenarios = scenarios if isinstance(scenarios, list) else []
    failed = [
        scenario
        for scenario in scenarios
        if isinstance(scenario, dict) and not scenario.get("passed", False)
    ]

    if failed:
        lines.append("")
        lines.append(t("cli.steprender.e2e.failed_header"))
        for scenario in failed[:E2E_MAX_FAILED_SCENARIOS]:
            lines.append(
                "  [red]✗[/red] {}".format(scenario.get("name", "?"))
            )
            if scenario.get("timed_out"):
                lines.append(
                    "    "
                    + t(
                        "cli.steprender.e2e.timed_out",
                        seconds=_e2e_clip(scenario.get("duration")),
                    )
                )
            if scenario.get("error"):
                lines.append(
                    "    "
                    + t("cli.steprender.e2e.error", message=_e2e_clip(scenario["error"]))
                )
            assertions = scenario.get("assertions")
            assertions = assertions if isinstance(assertions, list) else []
            failed_assertions = [
                assertion
                for assertion in assertions
                if isinstance(assertion, dict) and not assertion.get("passed", False)
            ]
            for assertion in failed_assertions[:E2E_MAX_ASSERTIONS_PER_SCENARIO]:
                lines.append(
                    "    "
                    + t(
                        "cli.steprender.e2e.assertion",
                        kind=assertion.get("kind", "?"),
                        tier=assertion.get("tier", "?"),
                    )
                )
                lines.append(
                    "      "
                    + t(
                        "cli.steprender.e2e.expected",
                        value=_e2e_clip(assertion.get("expected")),
                    )
                )
                lines.append(
                    "      "
                    + t(
                        "cli.steprender.e2e.actual",
                        value=_e2e_clip(assertion.get("actual")),
                    )
                )
            remaining = len(failed_assertions) - E2E_MAX_ASSERTIONS_PER_SCENARIO
            if remaining > 0:
                lines.append(
                    "    " + t("cli.steprender.e2e.more_assertions", count=remaining)
                )
        remaining_scenarios = len(failed) - E2E_MAX_FAILED_SCENARIOS
        if remaining_scenarios > 0:
            lines.append(
                "  " + t("cli.steprender.e2e.more_scenarios", count=remaining_scenarios)
            )

    artifacts = results.get("artifacts")
    if isinstance(artifacts, list) and artifacts:
        lines.append("")
        lines.append(t("cli.steprender.e2e.artifacts", count=len(artifacts)))

    render_full("\n".join(lines), title=t("cli.steprender.title.e2e"))


@register_renderer(StepType.SPEC_GATE)
def _render_spec_gate(step: Step) -> None:
    """Render the spec_gate result as a gate-conclusion summary.

    Mirrors the ``test`` step's summary-only presentation: when ``test_results``
    is present it reuses ``_build_test_summary_lines`` rather than dumping the
    raw pytest stdout/stderr. The gate conclusion (PASSED/FAILED, the fallback
    route to update_spec / implement, the no-op skip) is rendered first.
    """
    outputs = step.outputs or {}

    gate_passed = outputs.get("gate_passed", False)
    gate_route = outputs.get("gate_route", "")
    gate_skipped = outputs.get("gate_skipped", False)

    lines: list[str] = []

    # ── Gate conclusion ────────────────────────────────────────────
    if gate_skipped:
        lines.append(t("cli.steprender.gate.skipped"))
    elif gate_passed:
        lines.append(t("cli.steprender.status.passed"))
    else:
        lines.append(t("cli.steprender.status.failed"))

    if gate_route == "update_spec":
        lines.append(t("cli.steprender.gate.route_update_spec"))
    elif gate_route == "implement":
        lines.append(t("cli.steprender.gate.route_implement"))

    # ── Fix instructions (no raw test output) ──────────────────────
    fix_instructions = outputs.get("fix_instructions", "")
    if not gate_passed and fix_instructions:
        lines.append("")
        lines.append("[dim]" + "─" * 50 + "[/dim]")
        lines.append("")
        lines.append(f"  {fix_instructions}")

    # ── Test summary (re-test phase; summary only) ─────────────────
    test_results = outputs.get("test_results")
    if isinstance(test_results, dict) and test_results:
        lines.append("")
        lines.append("[dim]" + "─" * 50 + "[/dim]")
        lines.append("")
        lines.extend(_build_test_summary_lines(test_results))

    # ── Error ──────────────────────────────────────────────────────
    if step.error_message:
        lines.append("")
        lines.append(f"[bold red]{t('cli.steprender.error')}[/bold red] {step.error_message}")

    render_full("\n".join(lines), title=t("cli.steprender.title.spec_gate"))


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
        _default_render(step, t("cli.steprender.title.propose"))
        return

    # Render remaining outputs that aren't the proposal dict or its extracted sub-keys
    _PROPOSE_DEFERRED = {proposal_key, "summary", "files_to_modify", "files_to_create"}
    _render_remaining(step, t("cli.steprender.title.propose"), _PROPOSE_DEFERRED)


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
        _default_render(step, t("cli.steprender.title.design"))
        return

    # Render remaining outputs that aren't the design dict or its extracted sub-keys
    _DESIGN_DEFERRED = {design_key, "decisions", "components", "implementation_plan"}
    _render_remaining(step, t("cli.steprender.title.design"), _DESIGN_DEFERRED)


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
        render_full(t("cli.steprender.plan.proposal_line", proposal=proposal), title=t("cli.steprender.plan.proposal_title"))
        rendered_any = True

    # Section 2: Design
    if isinstance(design, dict):
        render_design(design)
        rendered_any = True
    elif isinstance(design, str) and design:
        render_full(t("cli.steprender.plan.design_line", design=design), title=t("cli.steprender.plan.design_title"))
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
            dep_str = ", ".join(str(d) for d in deps) if deps else t("cli.steprender.plan.deps_none")
            lines.append(
                t(
                    "cli.steprender.plan.group_line",
                    gid=gid,
                    name=name,
                    task_count=task_count,
                    total_loc=total_loc,
                    dep_str=dep_str,
                )
            )
        if lines:
            render_full("\n".join(lines), title=t("cli.steprender.plan.task_groups_title"))
            rendered_any = True

    if not rendered_any:
        _default_render(step, t("cli.steprender.title.plan"))
        return

    # Render any remaining keys not already covered
    _render_remaining(step, t("cli.steprender.title.plan"), {"plan", "task_groups", "total_complexity", "estimated_effort"})


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
        "complete": f"[green]{t('cli.steprender.implement.complete')}[/green]",
        "partial": f"[yellow]{t('cli.steprender.implement.partial')}[/yellow]",
        "failed": f"[red]{t('cli.steprender.implement.failed')}[/red]",
    }
    icon = status_icons.get(completion_status, "●")
    label = status_labels.get(completion_status, completion_status)

    groups_count = len(implemented_groups) if implemented_groups else 0
    files_count = len(files_changed) if files_changed else 0
    tests_count = len(tests_added) if tests_added else 0

    stats = [f"{icon} {label}"]
    if groups_count:
        stats.append(t("cli.steprender.implement.groups", count=groups_count))
    stats.append(t("cli.steprender.implement.files", count=files_count))
    if tests_count:
        stats.append(t("cli.steprender.implement.tests", count=tests_count))
    lines.append("  │  ".join(stats))

    # ── Summary ─────────────────────────────────────────────────────
    if summary:
        lines.append("")
        lines.append("[dim]" + "─" * 50 + "[/dim]")
        lines.append("")
        lines.append(f"[bold cyan]{t('cli.steprender.section.summary')}[/bold cyan]")
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
        lines.append(t("cli.steprender.implement.files_changed", count=files_count))
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
        lines.append(t("cli.steprender.implement.tests_added", count=tests_count))
        for test_path in tests_added:
            lines.append(f"  [green]+[/green] {test_path}")

    # ── Incomplete Tasks ────────────────────────────────────────────
    if incomplete_tasks:
        lines.append("")
        lines.append("[dim]" + "─" * 50 + "[/dim]")
        lines.append("")
        lines.append(t("cli.steprender.implement.incomplete_tasks", count=len(incomplete_tasks)))
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
            lines.append(t("cli.steprender.implement.restricted_applied", count=len(restricted_applied)))
        if restricted_failed:
            lines.append(t("cli.steprender.implement.restricted_failed", count=len(restricted_failed)))
            for edit in restricted_failed:
                if isinstance(edit, dict):
                    lines.append(f"  • {edit.get('file', edit.get('path', str(edit)))}")
                else:
                    lines.append(f"  • {edit}")

    # ── Error ───────────────────────────────────────────────────────
    if step.error_message:
        lines.append("")
        lines.append(f"[bold red]{t('cli.steprender.error')}[/bold red] {step.error_message}")

    render_full("\n".join(lines), title=t("cli.steprender.title.implement"))


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
        lines.append(f"[bold cyan]{t('cli.steprender.section.reasoning')}[/bold cyan]")
        lines.append(f"  {reasoning}")

    # ── Relevant Spec Items ────────────────────────────────────────
    selected_items = outputs.get("selected_items", [])
    if selected_items:
        lines.append("")
        lines.append("[dim]" + "─" * 50 + "[/dim]")
        lines.append("")
        lines.append(f"[bold yellow]{t('cli.steprender.analyze.relevant_spec')}[/bold yellow]")
        for item in selected_items:
            if isinstance(item, dict):
                spec = item.get("spec", "")
                name = item.get("requirement_name", "")
                label = f"{spec}:{name}" if spec and name else (spec or name or str(item))
            else:
                label = str(item)
            lines.append(f"  • {label}")

    # ── Error ──────────────────────────────────────────────────────
    if step.error_message:
        lines.append("")
        lines.append(f"[bold red]{t('cli.steprender.error')}[/bold red] {step.error_message}")

    render_full("\n".join(lines), title=t("cli.steprender.title.analyze"))


@register_renderer(StepType.INVESTIGATE)
def _render_investigate(step: Step) -> None:
    outputs = step.outputs or {}

    lines: list[str] = []

    # ── Verdict line ──────────────────────────────────────────────
    confidence = outputs.get("confidence", "?")
    iteration = outputs.get("investigation_iteration")
    if step.status == StepStatus.FAILED:
        lines.append(t("cli.steprender.status.failed"))
    elif outputs.get("conclusive"):
        lines.append(t("cli.steprender.investigate.conclusive", confidence=confidence))
    else:
        lines.append(
            t("cli.steprender.investigate.inconclusive", confidence=confidence)
        )
    if iteration:
        lines.append(t("cli.steprender.investigate.round", iteration=iteration))

    # ── Root cause ────────────────────────────────────────────────
    root_cause = outputs.get("root_cause", "")
    if root_cause:
        lines.append("")
        lines.append("[dim]" + "─" * 50 + "[/dim]")
        lines.append("")
        lines.append(
            f"[bold cyan]{t('cli.steprender.investigate.root_cause')}[/bold cyan]"
        )
        lines.append(f"  {root_cause}")

    # ── Evidence ──────────────────────────────────────────────────
    evidence = outputs.get("evidence") or []
    if isinstance(evidence, list) and evidence:
        lines.append("")
        lines.append(
            f"[bold yellow]{t('cli.steprender.investigate.evidence')}[/bold yellow]"
        )
        for item in evidence:
            lines.append(f"  • {item}")

    # ── Files involved ────────────────────────────────────────────
    files_involved = outputs.get("files_involved") or []
    if isinstance(files_involved, list) and files_involved:
        lines.append("")
        lines.append(
            f"[bold]{t('cli.steprender.investigate.files_involved')}[/bold]"
        )
        for path in files_involved:
            lines.append(f"  • {path}")

    # ── Suggested fix direction ───────────────────────────────────
    direction = outputs.get("suggested_fix_direction", "")
    if direction:
        lines.append("")
        lines.append(
            f"[bold green]{t('cli.steprender.investigate.suggested_fix')}[/bold green]"
        )
        lines.append(f"  {direction}")

    # ── Net-zero-diff violation ───────────────────────────────────
    workspace_delta = outputs.get("workspace_delta", "")
    if workspace_delta:
        lines.append("")
        lines.append(t("cli.steprender.investigate.workspace_dirty"))
        lines.append(f"  {workspace_delta}")

    # ── Error ─────────────────────────────────────────────────────
    if step.error_message:
        lines.append("")
        lines.append(
            f"[bold red]{t('cli.steprender.error')}[/bold red] {step.error_message}"
        )

    render_full("\n".join(lines), title=t("cli.steprender.title.investigate"))


@register_renderer(StepType.SELF_CHECK)
def _render_self_check(step: Step) -> None:
    outputs = step.outputs or {}

    lines: list[str] = []

    # ── Status line ───────────────────────────────────────────────
    actionable_count = outputs.get("actionable_count", 0)
    issues = outputs.get("issues", [])
    if step.status == StepStatus.FAILED:
        lines.append(t("cli.steprender.status.failed"))
    elif actionable_count == 0:
        lines.append(t("cli.steprender.status.passed"))
    else:
        lines.append(t("cli.steprender.self_check.actionable", count=actionable_count))

    # ── Summary ───────────────────────────────────────────────────
    result = outputs.get("self_check_result", {})
    summary = result.get("summary", "") if isinstance(result, dict) else ""
    if summary:
        lines.append("")
        lines.append("[dim]" + "─" * 50 + "[/dim]")
        lines.append("")
        lines.append(f"  {summary}")

    # ── Issues by severity ────────────────────────────────────────
    if issues and isinstance(issues, list):
        severity_styles = {
            "critical": ("[bold red]critical[/bold red]", "[red]"),
            "high": ("[bold red]high[/bold red]", "[red]"),
            "medium": ("[bold yellow]medium[/bold yellow]", "[yellow]"),
            "low": ("[dim]low[/dim]", "[dim]"),
        }

        lines.append("")
        lines.append("[dim]" + "─" * 50 + "[/dim]")
        lines.append("")

        from .steps._fix_context import extract_issue_display_fields

        for severity in ("critical", "high", "medium", "low"):
            group = [i for i in issues if isinstance(i, dict) and i.get("severity") == severity]
            if not group:
                continue
            _label, color = severity_styles.get(severity, ("[dim]?[/dim]", "[dim]"))
            sev_label = t(f"cli.steprender.severity.{severity}")
            lines.append(f"{color}{sev_label}[/{color[1:-1]}]  [dim]({len(group)})[/dim]")
            for issue in group:
                # Schema-compat: new self_check schema vs legacy verify_spec.
                _sev, desc, location = extract_issue_display_fields(issue)
                loc_suffix = f" [dim]@ {location}[/dim]" if location else ""
                lines.append(f"  {color}•[/{color[1:-1]}] {desc}{loc_suffix}")
            lines.append("")

    # ── Warning ───────────────────────────────────────────────────
    warning = outputs.get("warning", "")
    if warning:
        lines.append(f"[bold yellow]⚠ {warning}[/bold yellow]")

    # ── Error ─────────────────────────────────────────────────────
    if step.error_message:
        lines.append("")
        lines.append(f"[bold red]{t('cli.steprender.error')}[/bold red] {step.error_message}")

    render_full("\n".join(lines), title=t("cli.steprender.title.self_check"))


@register_renderer(StepType.VERIFY_SPEC)
def _render_verify_spec(step: Step) -> None:
    outputs = step.outputs or {}

    lines: list[str] = []

    # ── Verified status ────────────────────────────────────────────
    verified = outputs.get("verified", outputs.get("fix_needed") is not None and not outputs.get("fix_needed"))
    if verified:
        lines.append(t("cli.steprender.status.passed"))
    else:
        lines.append(t("cli.steprender.status.failed"))

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

        for scope_label, scope_key in [
            (t("cli.steprender.verify.in_scope"), "in_scope"),
            (t("cli.steprender.verify.out_of_scope"), "out_of_scope"),
        ]:
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
                prio_label = t(f"cli.steprender.severity.{prio}") if prio in priority_styles else prio
                lines.append(f"  {color}•[/{color[1:-1]}] [{prio_label}] {msg}")
                suggestion = issue.get("suggestion", "") if isinstance(issue, dict) else ""
                if suggestion:
                    lines.append(f"    [dim]→ {suggestion}[/dim]")

    # ── Recommendations ────────────────────────────────────────────
    recommendations = outputs.get("recommendations", []) or (verification_result.get("recommendations", []) if isinstance(verification_result, dict) else [])
    if recommendations and isinstance(recommendations, list):
        lines.append("")
        lines.append("[dim]" + "─" * 50 + "[/dim]")
        lines.append("")
        lines.append(f"[bold cyan]{t('cli.steprender.section.recommendations')}[/bold cyan]")
        for rec in recommendations:
            lines.append(f"  • {rec}")

    # ── Error ──────────────────────────────────────────────────────
    if step.error_message:
        lines.append("")
        lines.append(f"[bold red]{t('cli.steprender.error')}[/bold red] {step.error_message}")

    render_full("\n".join(lines), title=t("cli.steprender.title.verify_spec"))


@register_renderer(StepType.UPDATE_SPEC)
def _render_update_spec(step: Step) -> None:
    outputs = step.outputs or {}

    specs_updated = outputs.get("updated_specs", outputs.get("specs_updated", []))
    new_capabilities = outputs.get("new_capabilities", [])

    if not specs_updated and not new_capabilities:
        render_full(t("cli.steprender.update_spec.none"), title=t("cli.steprender.title.update_spec"))
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
        lines.append(f"[bold cyan]{t('cli.steprender.update_spec.new_capabilities')}[/bold cyan]")
        for cap in new_capabilities:
            lines.append(f"  • {cap}")

    # ── Error ──────────────────────────────────────────────────────
    if step.error_message:
        lines.append("")
        lines.append(f"[bold red]{t('cli.steprender.error')}[/bold red] {step.error_message}")

    render_full("\n".join(lines), title=t("cli.steprender.title.update_spec"))


@register_renderer(StepType.COMMIT)
def _render_commit(step: Step) -> None:
    outputs = step.outputs or {}

    committed = outputs.get("committed", False)

    # An error_message means the step failed; the no-op shortcut would bury the
    # diagnostic (e.g. a tag failure naming the tag and its target commit) behind
    # a misleading "No changes to commit".
    if not committed and not step.error_message:
        render_full(t("cli.steprender.commit.no_changes"), title=t("cli.steprender.title.commit"))
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
        lines.append(f"[bold red]{t('cli.steprender.error')}[/bold red] {step.error_message}")

    render_full("\n".join(lines), title=t("cli.steprender.title.commit"))
