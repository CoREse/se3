"""Display utilities for full-content text rendering.

Provides utilities for displaying LLM outputs, spec contents, proposals,
and design documents completely without truncation.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.markdown import Markdown
from rich.syntax import Syntax


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


def render_full(content: str, title: Optional[str] = None) -> None:
    """Render full content without truncation using Rich panels.

    Args:
        content: The text content to display
        title: Optional title for the panel
    """
    console = get_console()

    # Create panel with full content - no truncation
    panel = Panel(
        content,
        title=title,
        border_style="blue",
        expand=True,
    )
    console.print(panel)


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
    """Render plain text content with optional styling.

    Args:
        content: The text content to display
        title: Optional title for the panel
        style: Optional Rich style string for the content
    """
    console = get_console()

    if style:
        text = Text(content, style=style)
    else:
        text = content

    panel = Panel(
        text,
        title=title,
        border_style="blue",
        expand=True,
    )
    console.print(panel)


def render_code(content: str, language: str = "python", title: Optional[str] = None) -> None:
    """Render code content with syntax highlighting.

    Args:
        content: The code content to display
        language: Programming language for syntax highlighting
        title: Optional title for the panel
    """
    console = get_console()

    syntax = Syntax(content, language, theme="monokai", line_numbers=True)

    panel = Panel(
        syntax,
        title=title,
        border_style="green",
        expand=True,
    )
    console.print(panel)


def render_diff(diff_lines: list[str], file_path: str, max_lines: int = 50) -> None:
    """Render unified diff with red/green/cyan coloring using Rich.

    Args:
        diff_lines: Lines from difflib.unified_diff output
        file_path: File path for the panel title
        max_lines: Max displayable lines before truncation
    """
    console = get_console()
    text = Text()
    displayed = 0
    total = len(diff_lines)

    for line in diff_lines:
        # Skip the --- / +++ header lines (redundant with panel title)
        if line.startswith("--- ") or line.startswith("+++ "):
            continue

        if displayed >= max_lines:
            remaining = total - displayed
            text.append(f"\n... ({remaining} more lines)", style="dim")
            break

        if displayed > 0:
            text.append("\n")

        if line.startswith("@@"):
            text.append(line, style="cyan")
        elif line.startswith("-"):
            text.append(line, style="red")
        elif line.startswith("+"):
            text.append(line, style="green")
        else:
            text.append(line, style="dim")

        displayed += 1

    if displayed > 0:
        panel = Panel(
            text,
            title=file_path,
            border_style="yellow",
            expand=True,
        )
        console.print(panel)


def render_markdown(content: str, title: Optional[str] = None) -> None:
    """Render markdown content.

    Args:
        content: The markdown content to display
        title: Optional title for the panel
    """
    console = get_console()

    markdown = Markdown(content)

    panel = Panel(
        markdown,
        title=title,
        border_style="magenta",
        expand=True,
    )
    console.print(panel)
