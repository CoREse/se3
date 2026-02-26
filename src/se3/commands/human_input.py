"""Human input command for SE 3.0 tools.

Provides CLI for file-based human input:
- se3 human input --file <path>  : Read and process an input file
- se3 human input --list         : List pending input files
- se3 human input --read <id>    : Read a specific input by ID
"""

import json
from pathlib import Path
from typing import Optional, List, Dict, Any

import typer

from ..human_input import HumanInputStore, InputStatus, discover_human_inputs

app = typer.Typer(help="Human input file management")


def format_input_list(inputs: List[Any], format_type: str = "text") -> str:
    """Format a list of inputs for display."""
    if format_type == "json":
        return json.dumps([inp.to_dict() for inp in inputs], indent=2, default=str)

    if not inputs:
        return "No pending input files."

    lines = []
    lines.append(f"\n{'=' * 60}")
    lines.append("Pending Human Inputs")
    lines.append(f"{'=' * 60}")

    for inp in inputs:
        lines.append(f"\nID: {inp.id}")
        lines.append(f"Title: {inp.title}")
        lines.append(f"Created: {inp.created.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"Status: {inp.status.value}")
        if inp.context:
            preview = inp.context[:100].replace('\n', ' ')
            if len(inp.context) > 100:
                preview += "..."
            lines.append(f"Context: {preview}")
        lines.append("-" * 40)

    return "\n".join(lines)


def format_input_detail(inp: Any, format_type: str = "text") -> str:
    """Format a single input for display."""
    if format_type == "json":
        return json.dumps(inp.to_dict(), indent=2, default=str)

    lines = []
    lines.append(f"\n{'=' * 60}")
    lines.append(f"Human Input: {inp.id}")
    lines.append(f"{'=' * 60}")
    lines.append(f"\nTitle: {inp.title}")
    lines.append(f"Created: {inp.created.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Status: {inp.status.value}")

    if inp.context:
        lines.append(f"\n## Context\n{inp.context}")

    if inp.request:
        lines.append(f"\n## Request\n{inp.request}")

    if inp.response:
        lines.append(f"\n## Response\n{inp.response}")
        if inp.response_timestamp:
            lines.append(f"\n(Response written: {inp.response_timestamp.strftime('%Y-%m-%d %H:%M:%S')})")

    lines.append(f"\n{'=' * 60}\n")
    return "\n".join(lines)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    file_path: Optional[str] = typer.Option(
        None, "--file", "-f",
        help="Path to input file to read and process"
    ),
    list_pending: bool = typer.Option(
        False, "--list", "-l",
        help="List pending input files"
    ),
    read_id: Optional[str] = typer.Option(
        None, "--read", "-r",
        help="Read a specific input by ID"
    ),
    archive: bool = typer.Option(
        True, "--archive/--no-archive",
        help="Archive input file after processing (default: True)"
    ),
    inputs_dir: str = typer.Option(
        "human-inputs", "--inputs-dir", "-d",
        help="Directory containing input files"
    ),
    format_type: str = typer.Option(
        "text", "--format",
        help="Output format (text or json)"
    ),
):
    """Manage human input files.

    Examples:
        se3 human input --list                    # List pending inputs
        se3 human input --file path/to/input.md   # Process an input file
        se3 human input --read <id>               # Read a specific input
    """
    if ctx.invoked_subcommand is not None:
        return

    store = HumanInputStore(Path(inputs_dir))

    # List pending inputs
    if list_pending:
        inputs = store.get_pending_inputs()
        output = format_input_list(inputs, format_type)
        typer.echo(output)
        raise typer.Exit(code=0 if not inputs else 1)

    # Read specific input
    if read_id:
        inp = store.get_input(read_id)
        if not inp:
            typer.echo(f"Error: Input '{read_id}' not found", err=True)
            raise typer.Exit(code=1)
        output = format_input_detail(inp, format_type)
        typer.echo(output)
        raise typer.Exit(code=0)

    # Process input file
    if file_path:
        path = Path(file_path)
        if not path.exists():
            typer.echo(f"Error: File not found: {file_path}", err=True)
            raise typer.Exit(code=1)

        inp = store.read_input_file(path)
        if not inp:
            typer.echo(f"Error: Failed to parse input file: {file_path}", err=True)
            raise typer.Exit(code=1)

        # Display the input
        output = format_input_detail(inp, format_type)
        typer.echo(output)

        # If the file is outside the inputs_dir, copy it there for tracking
        if path.parent != store.inputs_dir:
            # Copy to inputs_dir
            dest_path = store.inputs_dir / path.name
            counter = 1
            while dest_path.exists():
                stem = path.stem
                if counter > 1:
                    stem = f"{path.stem}_{counter}"
                dest_path = store.inputs_dir / f"{stem}{path.suffix}"
                counter += 1

            import shutil
            shutil.copy2(str(path), str(dest_path))
            typer.echo(f"\n[Copied to {dest_path}]")

            # Re-parse from the new location
            inp = store.read_input_file(dest_path)

        raise typer.Exit(code=0)

    # No options provided, show help
    typer.echo(ctx.get_help())
    raise typer.Exit()


@app.command(name="template")
def create_template(
    title: str = typer.Argument(..., help="Title for the input"),
    context: str = typer.Option("", "--context", "-c", help="Context for the input"),
    request: str = typer.Option("", "--request", "-r", help="Request content"),
    inputs_dir: str = typer.Option("human-inputs", "--inputs-dir", "-d", help="Directory for input files"),
):
    """Create a new input file template.

    Example:
        se3 human input template "My Request" --context "Some context" --request "Do this"
    """
    store = HumanInputStore(Path(inputs_dir))
    filepath = store.create_input_template(title, context, request)
    typer.echo(f"Created input template: {filepath}")
    raise typer.Exit(code=0)


@app.command(name="respond")
def respond(
    input_id: str = typer.Argument(..., help="Input ID to respond to"),
    response: str = typer.Argument(..., help="Response content"),
    inputs_dir: str = typer.Option("human-inputs", "--inputs-dir", "-d", help="Directory containing input files"),
    archive_after: bool = typer.Option(True, "--archive/--no-archive", help="Archive after responding"),
):
    """Write a response to an input file.

    Example:
        se3 human input respond <id> "This is my response"
    """
    store = HumanInputStore(Path(inputs_dir))

    inp = store.get_input(input_id)
    if not inp:
        typer.echo(f"Error: Input '{input_id}' not found", err=True)
        raise typer.Exit(code=1)

    success = store.write_response(input_id, response)
    if not success:
        typer.echo(f"Error: Failed to write response", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Response written to {input_id}")

    if archive_after:
        if store.archive_input(input_id):
            typer.echo(f"Archived {input_id}")
        else:
            typer.echo(f"Warning: Failed to archive {input_id}", err=True)

    raise typer.Exit(code=0)
