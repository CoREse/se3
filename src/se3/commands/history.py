"""CLI command for browsing LLM chat history.

Usage:
    se3 history                          — List all flows with history
    se3 history <flow_id>                — Show all step conversations for a flow
    se3 history <flow_id> <step_type>    — Show detailed conversation for a step
"""

from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(help="Browse LLM chat history")


def _get_project_root() -> Path:
    """Get project root (same logic as other commands)."""
    return Path.cwd()


@app.callback(invoke_without_command=True)
def history_main(
    ctx: typer.Context,
    flow_id: Optional[str] = typer.Argument(None, help="Flow ID to inspect"),
    step_type: Optional[str] = typer.Argument(None, help="Step type to inspect (e.g. analyze, propose)"),
    format: str = typer.Option("text", "--format", "-f", help="Output format (text or json)"),
):
    """Browse LLM chat history for flow engine runs."""
    from ..engine.chat_history import (
        get_flow_history,
        get_step_history,
        list_flows,
        render_session_text,
    )
    import json as json_mod

    project_root = _get_project_root()

    if flow_id is None:
        # List all flows
        flows = list_flows(project_root)
        if not flows:
            typer.echo("No chat history found.")
            typer.echo(f"History is stored in se3/history/ after running 'se3 run'.")
            raise typer.Exit(0)

        if format == "json":
            typer.echo(json_mod.dumps({"flows": flows}, indent=2))
        else:
            typer.echo(f"Chat history: {len(flows)} flow(s)\n")
            for fid in flows:
                sessions = get_flow_history(project_root, fid)
                step_types = [s.step_type for s in sessions]
                msg_count = sum(len(s.messages) for s in sessions)
                typer.echo(f"  {fid}  ({len(sessions)} steps, {msg_count} messages)")
                if step_types:
                    typer.echo(f"    Steps: {', '.join(step_types)}")
            typer.echo(f"\nUse 'se3 history <flow_id>' for details.")
        raise typer.Exit(0)

    if step_type is None:
        # Show all steps for a flow
        sessions = get_flow_history(project_root, flow_id)
        if not sessions:
            typer.echo(f"No history found for flow '{flow_id}'.")
            raise typer.Exit(1)

        if format == "json":
            data = {
                "flow_id": flow_id,
                "sessions": [
                    {
                        "step_id": s.step_id,
                        "step_type": s.step_type,
                        "message_count": len(s.messages),
                    }
                    for s in sessions
                ],
            }
            typer.echo(json_mod.dumps(data, indent=2))
        else:
            typer.echo(f"Flow: {flow_id} ({len(sessions)} steps)\n")
            for session in sessions:
                typer.echo(render_session_text(session))
        raise typer.Exit(0)

    # Show specific step
    # Find the session by step_type
    sessions = get_flow_history(project_root, flow_id)
    matching = [s for s in sessions if s.step_type == step_type]

    if not matching:
        typer.echo(f"No history found for step '{step_type}' in flow '{flow_id}'.")
        available = [s.step_type for s in sessions]
        if available:
            typer.echo(f"Available steps: {', '.join(available)}")
        raise typer.Exit(1)

    session = matching[0]

    if format == "json":
        data = {
            "flow_id": flow_id,
            "step_id": session.step_id,
            "step_type": session.step_type,
            "messages": [m.to_dict() for m in session.messages],
        }
        typer.echo(json_mod.dumps(data, indent=2, ensure_ascii=False))
    else:
        typer.echo(render_session_text(session, truncate_prompt=0))

    raise typer.Exit(0)
