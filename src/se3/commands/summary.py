"""Summary command — generate project context summary.

Shares core logic with the PROJECT_SUMMARY flow step.
"""

import json
import sys
from pathlib import Path

import typer

app = typer.Typer(invoke_without_command=True)


@app.callback()
def summary(
    format: str = typer.Option("text", "--format", "-f", help="Output format (text or json)"),
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
):
    """Generate a project context summary using LLM.

    Collects project state (git, flow engine, backlog, specs) and
    produces a concise summary of the current project context.

    Examples:
        se3 summary
        se3 summary --format json
    """
    root = Path(project_root).resolve()

    try:
        from ..engine.steps.project_summary import generate_project_summary

        summary_text = generate_project_summary(root)

        if format == "json":
            print(json.dumps({"summary": summary_text}, indent=2))
        else:
            print(f"\n{'=' * 60}")
            print("SE3 Project Summary")
            print(f"{'=' * 60}\n")
            print(summary_text)
            print(f"\n{'=' * 60}\n")

    except Exception as e:
        print(f"Error generating summary: {e}", file=sys.stderr)
        raise typer.Exit(code=1)
