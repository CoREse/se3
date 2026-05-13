"""Display utilities for full-content text rendering.

Provides utilities for displaying LLM outputs, spec contents, proposals,
and design documents completely without truncation.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from rich.console import Console
from rich.style import Style
from rich.text import Text
from rich.markdown import Markdown
from rich.syntax import Syntax

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


# Fixed width (in characters) of the reverse-color block footer used as the
# bottom boundary of a rendered block. Kept small and constant so it never
# stretches with terminal width and never adds visible characters when copied.
_BLOCK_FOOTER_WIDTH = 4


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
        lines.append("[bold cyan]Summary:[/bold cyan]")
        lines.append(summary)
        lines.append("")

    # Files to modify
    files_to_modify = proposal.get("files_to_modify", [])
    if files_to_modify:
        lines.append("[bold yellow]Files to Modify:[/bold yellow]")
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
        lines.append("[bold green]Files to Create:[/bold green]")
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
        lines.append("[bold magenta]Rationale:[/bold magenta]")
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
    render_full(content, title="Proposal")


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
        lines.append("[bold cyan]Overview[/bold cyan]")
        lines.append(overview)
        lines.append("")

    # Components section
    components = design.get("components", [])
    if components:
        lines.append("[bold yellow]Components[/bold yellow]")
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
        lines.append("[bold green]Interfaces[/bold green]")
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
        lines.append("[bold magenta]Key Decisions[/bold magenta]")
        for decision in decisions:
            if isinstance(decision, dict):
                decision_text = decision.get("decision", "")
                reason = decision.get("reason", "")
                lines.append(f"\n• {decision_text}")
                if reason:
                    lines.append(f"  [dim]Reason: {reason}[/dim]")
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
    render_full(content, title="Design Document")


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
        lines.append(f"[bold cyan]Title:[/bold cyan] {title}")
    if version:
        lines.append(f"[bold cyan]Version:[/bold cyan] {version}")
    if spec_type:
        lines.append(f"[bold cyan]Type:[/bold cyan] {spec_type}")

    if title or version or spec_type:
        lines.append("")
        lines.append("─" * 40)
        lines.append("")

    # Description
    description = spec.get("description", "")
    if description:
        lines.append("[bold yellow]Description[/bold yellow]")
        lines.append(description)
        lines.append("")

    # Requirements section
    requirements = spec.get("requirements", [])
    if requirements:
        lines.append("[bold green]Requirements[/bold green]")
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
    render_full(content, title="Spec Content")


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
            text.append(f"\n... ({remaining} more lines)", style="dim")
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
        console.print(_reverse_title(f"Diff: {file_path}", "yellow"))
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
