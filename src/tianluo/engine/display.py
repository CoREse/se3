"""Display utilities for full-content text rendering.

Provides utilities for displaying LLM outputs, spec contents, proposals,
and design documents completely without truncation.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from rich.console import Console
from rich.style import Style
from rich.table import Table
from rich.text import Text
from rich.markdown import Markdown
from rich.syntax import Syntax

from ..i18n import t
from ..usage import UsageRecord, UsageStatus, UsageSummary, estimate_record_cost
from .token_usage import UsageTotals, format_cost

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


# Fixed width (in characters) of the reverse-color block footer used as the
# bottom boundary of a rendered block. Kept small and constant so it never
# stretches with terminal width and never adds visible characters when copied.
_BLOCK_FOOTER_WIDTH = 4


# Role color for the token-usage summary block. Cyan reads as an auxiliary /
# summary accent (see the spec's color→role table), keeping the block legible
# yet unobtrusive — it summarizes, it does not steal the show.
_USAGE_BLOCK_COLOR = "cyan"


# Global console instance for consistent output
_console: Optional[Console] = None


def get_console() -> Console:
    """Get or create the global console instance."""
    global _console
    if _console is None:
        _console = Console()
    return _console


def set_console(console: Console) -> None:
    """Set the global console instance (useful for testing)."""
    global _console
    _console = console


def _reverse_title(title: str, color: str) -> Text:
    """Build a reverse-color block heading: ` ## Title ` on a colored background.

    Uses an explicit ``Style`` so the title text is not parsed as Rich markup
    (square brackets in the title are safe). Returns a ``Text`` object.
    """
    style = Style(color="white", bgcolor=color, bold=True)
    return Text(f" ## {title} ", style=style)


def _reverse_footer(color: str, width: int = _BLOCK_FOOTER_WIDTH) -> Text:
    """Build a fixed-width reverse-color block as the bottom boundary marker.

    The body is `width` spaces — when copied from the terminal, the user gets
    only whitespace (no visible boundary characters).
    """
    style = Style(bgcolor=color)
    return Text(" " * width, style=style)


def render_block_header(title: str, color: str) -> None:
    """Print a reverse-color block heading followed by a blank line.

    Public thin wrapper for modules outside ``display.py`` so they don't need
    to assemble Rich markup strings themselves.
    """
    console = get_console()
    console.print(_reverse_title(title, color))
    console.print("")


def render_block_footer(color: str) -> None:
    """Print the fixed-width reverse-color block footer followed by a blank line.

    Caller is responsible for any preceding blank line separating content from
    the footer.
    """
    console = get_console()
    console.print(_reverse_footer(color))
    console.print("")


def render_usage_block(
    totals: Any, title: Optional[str] = None
) -> None:
    """Render an aligned token / cost summary as a reverse-color block.

    Accepts a :class:`~tianluo.engine.token_usage.UsageTotals`, the JSON-primitive
    dict it serializes to (as persisted in ``step.outputs['token_usage']`` and
    ``State.session_token_usage``), or ``None``. Empty or ``None`` usage renders
    **nothing**, so callers need not guard themselves.

    The body is a fixed two-column table — left-aligned dim labels, right-aligned
    values — so the numbers line up regardless of magnitude. Costs render as
    ``$0.0000`` and token counts carry thousands separators. The block follows
    the standard reverse-color visual (title block / body / fixed-width footer,
    no Panel, no Rule).

    Args:
        totals: A ``UsageTotals``, a usage dict, or ``None``.
        title: Heading shown in the reverse-color title block. ``None`` uses the
            i18n-rendered default ("Token Usage"); resolved here rather than in
            the signature default so language selection stays lazy per-call.
    """
    if totals is None:
        return
    if not isinstance(totals, UsageTotals):
        totals = UsageTotals.from_dict(totals)
    if totals.is_empty():
        return

    if title is None:
        title = t("cli.display.usage.title")

    rows = [
        (t("cli.display.usage.input_tokens"), f"{totals.input_tokens:,}"),
        (t("cli.display.usage.output_tokens"), f"{totals.output_tokens:,}"),
        (t("cli.display.usage.cache_read"), f"{totals.cache_read_input_tokens:,}"),
        (t("cli.display.usage.cache_creation"), f"{totals.cache_creation_input_tokens:,}"),
        (t("cli.display.usage.cost"), format_cost(totals.total_cost_usd)),
    ]
    _render_usage_rows(rows, title)


def _render_usage_rows(rows: list[tuple[str, str]], title: str) -> None:
    """Render the shared fixed two-column usage table body.

    Left-aligned dim labels, right-aligned values, standard reverse-color
    title/footer chrome — the one visual shape every usage block shares so
    numbers line up across step and session views.
    """
    label_w = max(len(label) for label, _ in rows)
    value_w = max(len(value) for _, value in rows)

    lines = [
        f"  [dim]{label.ljust(label_w)}[/dim]  {value.rjust(value_w)}"
        for label, value in rows
    ]

    console = get_console()
    console.print(_reverse_title(title, _USAGE_BLOCK_COLOR))
    console.print("")
    console.print("\n".join(lines))
    console.print("")
    console.print(_reverse_footer(_USAGE_BLOCK_COLOR))
    console.print("")


def _format_optional_cost(value: Any) -> str:
    """Render a cost that may be unknown — unknown shows "Unknown", not $0."""
    if value is None:
        return t("cli.display.usage.unknown_cost")
    return format_cost(value)


def render_usage_summary_block(
    summary: Any, title: Optional[str] = None
) -> None:
    """Render a session usage/cost summary from the shared UsageSummary backend.

    Shows token totals plus the two cost columns — provider actual and
    estimated — kept separate so the display never fabricates a single
    "total" that mixes both. Unknown-usage/model/price/cache-TTL call counts
    and the completeness label surface whatever could not be measured;
    nothing renders when the flow consumed no LLM calls at all.

    Accepts a :class:`~tianluo.usage.UsageSummary`, its persisted dict, or
    ``None``.
    """
    if summary is None:
        return
    if not isinstance(summary, UsageSummary):
        summary = UsageSummary.from_dict(summary)
    if not summary.records and not _summary_has_measurements(summary):
        # A records-free (wire-shaped) summary still carries its aggregate
        # totals and cost columns — the per-step block persists exactly that
        # shape, so gating on the record list alone would hide it.
        return

    if title is None:
        title = t("cli.run.session_usage_title")

    console = get_console()
    for renderable in _usage_summary_renderable(summary, title):
        console.print(renderable)


def _summary_has_measurements(summary: UsageSummary) -> bool:
    """True when a records-free summary still carries something to show."""
    totals = summary.totals
    return bool(
        totals.logical_input_tokens
        or totals.output_tokens
        or totals.cache_read_input_tokens
        or totals.cache_creation_total_input_tokens
        or summary.actual_cost_usd
        or summary.estimated_cost_usd
        or summary.unknown_call_count
    )


def _usage_status_label(status: Any) -> str:
    """Render one UsageStatus value via i18n; unknown values stay verbatim.

    Accepts both a raw wire string and a ``UsageStatus`` member. WHY the
    ``getattr(..., "value")`` hop: ``UsageStatus`` is a ``(str, Enum)`` mixin,
    so ``str(member)`` yields ``'UsageStatus.AVAILABLE'`` — feeding that to the
    constructor raises and would print the Python repr instead of the label.
    """
    raw = getattr(status, "value", status)
    try:
        parsed = UsageStatus(raw)
    except (TypeError, ValueError):
        return str(raw)
    return t(f"usage.status.{parsed.value}")


def _usage_summary_renderable(summary: UsageSummary, title: str) -> list:
    """The renderable half of the summary block, shared with history display.

    Returns the reverse-color title / rows / footer as a list of rich objects
    (built without printing) so callers with their own console can render it,
    while :func:`render_usage_summary_block` prints the same list.
    """
    totals = summary.totals
    rows = [
        (t("cli.display.usage.input_tokens"), f"{totals.logical_input_tokens:,}"),
        (t("cli.display.usage.output_tokens"), f"{totals.output_tokens:,}"),
        (t("cli.display.usage.cache_read"), f"{totals.cache_read_input_tokens:,}"),
        (
            t("cli.display.usage.cache_creation"),
            f"{totals.cache_creation_total_input_tokens:,}",
        ),
        (t("cli.display.usage.actual_cost"), _format_optional_cost(summary.actual_cost_usd)),
        (
            t("cli.display.usage.estimated_cost"),
            _format_optional_cost(summary.estimated_cost_usd),
        ),
        (
            t("cli.display.usage.completeness"),
            t(
                "cli.display.usage.completeness_complete"
                if summary.completeness == "complete"
                else "cli.display.usage.completeness_partial"
            ),
        ),
    ]
    for label, count in (
        (t("cli.display.usage.unknown_calls"), summary.unknown_call_count),
        (t("cli.display.usage.unknown_model"), summary.unknown_model_count),
        (t("cli.display.usage.unknown_price"), summary.unknown_price_count),
        (t("cli.display.usage.unknown_cache_ttl"), summary.unknown_cache_ttl_count),
    ):
        if count:
            rows.append((label, f"{count:,}"))

    label_w = max(len(label) for label, _ in rows)
    value_w = max(len(value) for _, value in rows)
    lines = [
        f"  [dim]{label.ljust(label_w)}[/dim]  {value.rjust(value_w)}"
        for label, value in rows
    ]
    return [
        _reverse_title(title, _USAGE_BLOCK_COLOR),
        Text(""),
        Text("\n".join(lines)),
        Text(""),
        _reverse_footer(_USAGE_BLOCK_COLOR),
        Text(""),
    ]


def build_history_usage_renderables(
    payload: Optional[Dict[str, Any]], catalog: Any = None
) -> list:
    """Return Rich renderables for a :func:`~tianluo.usage.build_usage_payload` dict.

    The caller prints the returned list with its own console (``luo history
    show`` owns one), mirroring :func:`render_session_detailed`.  The payload
    is the same structured summary the ``--json`` path emits, so text and JSON
    views can never drift; ``catalog`` (optional) is the pricing table used
    for the per-call estimate column.  Returns ``[]`` for a missing payload
    or a flow with no recorded usage at all.
    """
    if not isinstance(payload, dict):
        return []
    calls = payload.get("calls")
    steps = payload.get("steps")
    if not isinstance(calls, list) and not isinstance(steps, dict):
        return []
    if not calls and not steps:
        return []

    renderables: list = [Text("")]

    if isinstance(calls, list) and calls:
        renderables.append(Text(t("history.usage.calls_header"), style="bold"))
        table = Table()
        table.add_column(t("history.usage.col.call"), justify="right")
        table.add_column(t("history.usage.col.agent"))
        table.add_column(t("history.usage.col.runner"))
        table.add_column(t("history.usage.col.provider"))
        table.add_column(t("history.usage.col.model"))
        table.add_column(t("history.usage.col.status"))
        table.add_column(t("history.usage.col.input"), justify="right")
        table.add_column(t("history.usage.col.output"), justify="right")
        table.add_column(t("history.usage.col.cache_read"), justify="right")
        table.add_column(t("history.usage.col.cache_create"), justify="right")
        table.add_column(t("history.usage.col.actual"), justify="right")
        table.add_column(t("history.usage.col.estimate"), justify="right")
        for index, raw in enumerate(calls, 1):
            if not isinstance(raw, dict):
                continue
            record = UsageRecord.from_dict(raw)
            # ``resolved_model`` carries the internal "unknown" sentinel when
            # the model could not be resolved; the user-visible placeholder
            # must follow the UI language like the adjacent cells do.
            resolved = record.resolved_model
            model = (
                resolved
                if resolved and resolved != "unknown"
                else t("cli.display.usage.model_unknown")
            )
            if record.reported_model and record.reported_model != model:
                model = f"{model} ({record.reported_model})"
            actual = _format_optional_cost(record.actual_cost_usd)
            # Prefer the estimate embedded by build_usage_payload — the one
            # shared backend result the WebUI also renders — so the two
            # surfaces can never disagree on the same call's figure.
            embedded_estimate = raw.get("estimated_cost_usd")
            estimate_label = t("cli.display.usage.unknown_cost")
            if embedded_estimate is not None:
                estimate_label = format_cost(embedded_estimate)
            else:
                estimate = estimate_record_cost(record, catalog)
                if estimate.is_estimated:
                    estimate_label = format_cost(estimate.estimated_cost_usd)
            table.add_row(
                f"{index}:{record.attempt}",
                record.agent_name or "-",
                record.runner_type or "-",
                record.provider or "-",
                model,
                _usage_status_label(record.usage_status),
                f"{record.logical_input_tokens:,}",
                f"{record.output_tokens:,}",
                f"{record.cache_read_input_tokens:,}",
                f"{record.cache_creation_total_input_tokens:,}",
                actual,
                estimate_label,
            )
        renderables.append(table)
        renderables.append(Text(""))

    if isinstance(steps, dict) and steps:
        renderables.append(Text(t("history.usage.steps_header"), style="bold"))
        step_table = Table()
        step_table.add_column(t("history.usage.col.step"))
        step_table.add_column(t("history.usage.col.calls"), justify="right")
        step_table.add_column(t("history.usage.col.input"), justify="right")
        step_table.add_column(t("history.usage.col.output"), justify="right")
        step_table.add_column(t("history.usage.col.actual"), justify="right")
        step_table.add_column(t("history.usage.col.estimate"), justify="right")
        step_table.add_column(t("history.usage.col.completeness"))
        for step_key, entry in steps.items():
            if not isinstance(entry, dict):
                continue
            summary = UsageSummary.from_dict(entry.get("summary"))
            step_table.add_row(
                str(step_key),
                str(entry.get("record_count") or 0),
                f"{summary.totals.logical_input_tokens:,}",
                f"{summary.totals.output_tokens:,}",
                _format_optional_cost(summary.actual_cost_usd),
                _format_optional_cost(summary.estimated_cost_usd),
                t(
                    "usage.completeness_complete"
                    if summary.completeness == "complete"
                    else "usage.completeness_partial"
                ),
            )
        renderables.append(step_table)
        renderables.append(Text(""))

    summary = payload.get("summary")
    if isinstance(summary, dict):
        summary_obj = UsageSummary.from_dict(summary)
        renderables.append(Text(t("history.usage.flow_header"), style="bold"))
        # The boxed summary inside the flow-totals region must carry a
        # flow-level title — reusing the per-session title here would present
        # flow totals under a "session usage" label.
        renderables.extend(
            _usage_summary_renderable(
                summary_obj, t("history.usage.flow_totals_title")
            )
        )
    if payload.get("legacy"):
        renderables.append(
            Text(t("history.usage.legacy_note"), style="dim")
        )
    return renderables


def render_full(content: str, title: Optional[str] = None) -> None:
    """Render full content without truncation, left-aligned with a markdown-style title.

    Args:
        content: The text content to display
        title: Optional bold blue ``## Title`` heading printed above the content
    """
    console = get_console()

    if title:
        console.print(_reverse_title(title, "blue"))
        console.print("")
    console.print(content)
    console.print("")
    if title:
        console.print(_reverse_footer("blue"))
        console.print("")


def render_proposal(proposal: Dict[str, Any]) -> None:
    """Render proposal content with appropriate styling.

    Args:
        proposal: Proposal dictionary containing fields like summary,
                 files_to_modify, files_to_create, etc.
    """
    console = get_console()

    # Build formatted content
    lines = []

    # Summary section
    summary = proposal.get("summary", "")
    if summary:
        lines.append(f"[bold cyan]{t('cli.display.proposal.summary')}[/bold cyan]")
        lines.append(summary)
        lines.append("")

    # Files to modify
    files_to_modify = proposal.get("files_to_modify", [])
    if files_to_modify:
        lines.append(f"[bold yellow]{t('cli.display.proposal.files_to_modify')}[/bold yellow]")
        for f in files_to_modify:
            if isinstance(f, dict):
                path = f.get("path", "")
                reason = f.get("reason", "")
                lines.append(f"  • {path}")
                if reason:
                    lines.append(f"    [dim]{reason}[/dim]")
            else:
                lines.append(f"  • {f}")
        lines.append("")

    # Files to create
    files_to_create = proposal.get("files_to_create", [])
    if files_to_create:
        lines.append(f"[bold green]{t('cli.display.proposal.files_to_create')}[/bold green]")
        for f in files_to_create:
            if isinstance(f, dict):
                path = f.get("path", "")
                purpose = f.get("purpose", "")
                lines.append(f"  • {path}")
                if purpose:
                    lines.append(f"    [dim]{purpose}[/dim]")
            else:
                lines.append(f"  • {f}")
        lines.append("")

    # Rationale
    rationale = proposal.get("rationale", "")
    if rationale:
        lines.append(f"[bold magenta]{t('cli.display.proposal.rationale')}[/bold magenta]")
        lines.append(rationale)
        lines.append("")

    # Additional fields
    for key, value in proposal.items():
        if key not in ("summary", "files_to_modify", "files_to_create", "rationale"):
            if value:
                lines.append(f"[bold]{key.replace('_', ' ').title()}:[/bold]")
                if isinstance(value, list):
                    for item in value:
                        lines.append(f"  • {item}")
                else:
                    lines.append(str(value))
                lines.append("")

    content = "\n".join(lines)
    render_full(content, title=t("cli.display.proposal.title"))


def render_design(design: Dict[str, Any]) -> None:
    """Render design document content with section headers.

    Args:
        design: Design document dictionary containing sections like
                overview, components, interfaces, decisions, etc.
    """
    console = get_console()

    # Build formatted content with section headers
    lines = []

    # Overview section
    overview = design.get("overview", "")
    if overview:
        lines.append(f"[bold cyan]{t('cli.display.design.overview')}[/bold cyan]")
        lines.append(overview)
        lines.append("")

    # Components section
    components = design.get("components", [])
    if components:
        lines.append(f"[bold yellow]{t('cli.display.design.components')}[/bold yellow]")
        for comp in components:
            if isinstance(comp, dict):
                name = comp.get("name", "")
                desc = comp.get("description", "")
                lines.append(f"\n[bold]{name}[/bold]")
                if desc:
                    lines.append(desc)
            else:
                lines.append(f"  • {comp}")
        lines.append("")

    # Interfaces section
    interfaces = design.get("interfaces", [])
    if interfaces:
        lines.append(f"[bold green]{t('cli.display.design.interfaces')}[/bold green]")
        for iface in interfaces:
            if isinstance(iface, dict):
                name = iface.get("name", "")
                signature = iface.get("signature", "")
                desc = iface.get("description", "")
                lines.append(f"\n[bold]{name}[/bold]")
                if signature:
                    lines.append(f"[dim]{signature}[/dim]")
                if desc:
                    lines.append(desc)
            else:
                lines.append(f"  • {iface}")
        lines.append("")

    # Key Decisions section
    decisions = design.get("decisions", [])
    if decisions:
        lines.append(f"[bold magenta]{t('cli.display.design.key_decisions')}[/bold magenta]")
        for decision in decisions:
            if isinstance(decision, dict):
                decision_text = decision.get("decision", "")
                reason = decision.get("reason", "")
                lines.append(f"\n• {decision_text}")
                if reason:
                    lines.append(f"  [dim]{t('cli.display.design.reason')}{reason}[/dim]")
            else:
                lines.append(f"  • {decision}")
        lines.append("")

    # Additional sections
    for key, value in design.items():
        if key not in ("overview", "components", "interfaces", "decisions"):
            if value:
                lines.append(f"[bold]{key.replace('_', ' ').title()}[/bold]")
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            item_str = str(item.get("name", item.get("title", str(item))))
                            lines.append(f"  • {item_str}")
                        else:
                            lines.append(f"  • {item}")
                elif isinstance(value, dict):
                    for k, v in value.items():
                        lines.append(f"  [bold]{k}:[/bold] {v}")
                else:
                    lines.append(str(value))
                lines.append("")

    content = "\n".join(lines)
    render_full(content, title=t("cli.display.design.title"))


def render_spec_content(spec: Dict[str, Any]) -> None:
    """Render spec content with metadata display.

    Args:
        spec: Spec dictionary containing fields like title, version,
              description, requirements, etc.
    """
    console = get_console()

    # Build formatted content
    lines = []

    # Metadata header
    title = spec.get("title", "")
    version = spec.get("version", "")
    spec_type = spec.get("type", "")

    if title:
        lines.append(f"[bold cyan]{t('cli.display.spec.field_title')}[/bold cyan] {title}")
    if version:
        lines.append(f"[bold cyan]{t('cli.display.spec.field_version')}[/bold cyan] {version}")
    if spec_type:
        lines.append(f"[bold cyan]{t('cli.display.spec.field_type')}[/bold cyan] {spec_type}")

    if title or version or spec_type:
        lines.append("")
        lines.append("─" * 40)
        lines.append("")

    # Description
    description = spec.get("description", "")
    if description:
        lines.append(f"[bold yellow]{t('cli.display.spec.description')}[/bold yellow]")
        lines.append(description)
        lines.append("")

    # Requirements section
    requirements = spec.get("requirements", [])
    if requirements:
        lines.append(f"[bold green]{t('cli.display.spec.requirements')}[/bold green]")
        for req in requirements:
            if isinstance(req, dict):
                req_id = req.get("id", "")
                req_desc = req.get("description", "")
                priority = req.get("priority", "")
                priority_tag = f" [dim]({priority})[/dim]" if priority else ""
                lines.append(f"\n[bold]{req_id}[/bold]{priority_tag}")
                if req_desc:
                    lines.append(req_desc)
            else:
                lines.append(f"  • {req}")
        lines.append("")

    # Additional sections
    for key, value in spec.items():
        if key not in ("title", "version", "type", "description", "requirements"):
            if value:
                lines.append(f"[bold]{key.replace('_', ' ').title()}[/bold]")
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            item_lines = []
                            for k, v in item.items():
                                if k in ("name", "title", "id"):
                                    item_lines.insert(0, f"[bold]{v}[/bold]")
                                else:
                                    item_lines.append(f"  {k}: {v}")
                            lines.extend(item_lines)
                        else:
                            lines.append(f"  • {item}")
                elif isinstance(value, dict):
                    for k, v in value.items():
                        lines.append(f"  [bold]{k}:[/bold] {v}")
                else:
                    lines.append(str(value))
                lines.append("")

    content = "\n".join(lines)
    render_full(content, title=t("cli.display.spec.title"))


def render_text(content: str, title: Optional[str] = None, style: Optional[str] = None) -> None:
    """Render plain text content left-aligned with optional styling.

    Args:
        content: The text content to display
        title: Optional bold blue ``## Title`` heading printed above the content
        style: Optional Rich style string applied to the content
    """
    console = get_console()

    if title:
        console.print(_reverse_title(title, "blue"))
        console.print("")

    if style:
        console.print(Text(content, style=style))
    else:
        console.print(content)
    console.print("")
    if title:
        console.print(_reverse_footer("blue"))
        console.print("")


def render_code(content: str, language: str = "python", title: Optional[str] = None) -> None:
    """Render code content with syntax highlighting, left-aligned.

    Args:
        content: The code content to display
        language: Programming language for syntax highlighting
        title: Optional bold green ``## Title`` heading printed above the code
    """
    console = get_console()

    if title:
        console.print(_reverse_title(title, "green"))
        console.print("")

    syntax = Syntax(content, language, theme="monokai", line_numbers=True)
    console.print(syntax)
    console.print("")
    if title:
        console.print(_reverse_footer("green"))
        console.print("")


def render_diff(diff_lines: list[str], file_path: str, max_lines: int = 50) -> None:
    """Render unified diff with red/green/cyan coloring and line numbers.

    Args:
        diff_lines: Lines from difflib.unified_diff output
        file_path: File path used in the heading
        max_lines: Max displayable lines before truncation
    """
    console = get_console()
    text = Text()
    displayed = 0
    total = sum(1 for l in diff_lines if not l.startswith('--- ') and not l.startswith('+++ '))

    old_line_no = 0
    new_line_no = 0
    # Width for line number columns (fixed for alignment)
    lno_width = 4

    for line in diff_lines:
        # Skip the --- / +++ header lines (redundant with the heading)
        if line.startswith("--- ") or line.startswith("+++ "):
            continue

        if displayed >= max_lines:
            remaining = total - displayed
            text.append("\n" + t("cli.display.diff.more_lines", remaining=remaining), style="dim")
            break

        if displayed > 0:
            text.append("\n")

        if line.startswith("@@"):
            # Parse hunk header to extract starting line numbers
            m = _HUNK_HEADER_RE.match(line)
            if m:
                old_line_no = int(m.group(1))
                new_line_no = int(m.group(2))
            text.append(line, style="cyan")
        elif line.startswith("-"):
            text.append(f"{old_line_no:>{lno_width}} ", style="dim")
            text.append(line, style="red")
            old_line_no += 1
        elif line.startswith("+"):
            text.append(f"{new_line_no:>{lno_width}} ", style="dim")
            text.append(line, style="green")
            new_line_no += 1
        else:
            # Context line — show new-file line number
            text.append(f"{new_line_no:>{lno_width}} ", style="dim")
            text.append(line, style="dim")
            old_line_no += 1
            new_line_no += 1

        displayed += 1

    if displayed > 0:
        console.print(_reverse_title(t("cli.display.diff.heading", file_path=file_path), "yellow"))
        console.print("")
        console.print(text)
        console.print("")
        console.print(_reverse_footer("yellow"))
        console.print("")


def render_markdown(content: str, title: Optional[str] = None) -> None:
    """Render markdown content left-aligned.

    Args:
        content: The markdown content to display
        title: Optional bold magenta ``## Title`` heading printed above the content
    """
    console = get_console()

    if title:
        console.print(_reverse_title(title, "magenta"))
        console.print("")

    markdown = Markdown(content)
    console.print(markdown)
    console.print("")
    if title:
        console.print(_reverse_footer("magenta"))
        console.print("")
