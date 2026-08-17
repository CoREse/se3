"""SE3 History command — View and manage session history.

Provides commands to list flows, show flow details, restore previous sessions,
and list archived flows.

Usage:
    luo history                          # List all flows
    luo history list                     # List all flows
    luo history show <flow_id>           # Show flow details
    luo history restore <flow_id>        # Restore a flow
    luo history archived                 # List archived flows
"""

from __future__ import annotations
from tianluo.runtime_paths import runtime_dir

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer
from rich.console import Console
from rich.table import Table

from ..i18n import t, t_status

# Import necessary modules from the engine
from ..engine.persistence import PersistenceManager
from ..engine.chat_history import (
    list_flows as list_chat_flows,
    collect_usage_records_from_sessions,
    get_flow_history,
    get_detailed_json,
    interleave_sessions_for_display,
    render_session_detailed,
)
from ..engine.display import build_history_usage_renderables
from ..strategy_view import plan_mode_view, scope_view
from ..usage import (
    UsageRecord,
    build_usage_payload,
    legacy_usage_record,
)

app = typer.Typer(help=t("cli.help.history"))
console = Console()


def get_project_root() -> Path:
    """Find project root by looking for .git directory or an SE3 config file.

    Binds the i18n language to the discovered root: the import-time help strings
    resolve the language singleton from the cwd, which can sit below the project
    root, so it must be re-resolved once the target project is known.
    """
    from ..config import is_se3_project_root
    from ..i18n import bind_project_root

    cwd = Path.cwd()
    root = cwd
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".git").exists() or is_se3_project_root(parent):
            root = parent
            break
    bind_project_root(root)
    return root


def format_datetime(iso_string: str) -> str:
    """Format ISO datetime string to human-readable format."""
    try:
        dt = datetime.fromisoformat(iso_string)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_string


def format_duration(created: str, updated: str) -> str:
    """Calculate and format duration between two timestamps."""
    try:
        dt_created = datetime.fromisoformat(created)
        dt_updated = datetime.fromisoformat(updated)
        duration = dt_updated - dt_created

        total_minutes = int(duration.total_seconds() / 60)
        if total_minutes < 60:
            return f"{total_minutes}m"
        hours = total_minutes // 60
        minutes = total_minutes % 60
        if hours < 24:
            return f"{hours}h {minutes}m"
        days = hours // 24
        hours = hours % 24
        return f"{days}d {hours}h"
    except (ValueError, TypeError):
        return "unknown"


def list_all_flows(project_root: Path) -> List[Dict[str, Any]]:
    """List all flows from all data sources."""
    persistence = PersistenceManager(project_root)
    return persistence.list_all_flows()


def list_archived_flows_from_disk(project_root: Path) -> List[Dict[str, Any]]:
    """List all archived flows from the archive directory."""
    archive_dir = runtime_dir(project_root) / "state" / "archive"
    if not archive_dir.exists():
        return []

    from ..engine.persistence import _read_snapshot_header

    archived = []
    for archive_file in sorted(archive_dir.glob("engine_*.json")):
        try:
            # Extract timestamp from filename. clear_state resolves same-second
            # archive-name collisions by appending a numeric suffix
            # (engine_<ts>_<n>.json, persistence.py); parse only the leading
            # YYYYMMDD_HHMMSS and tolerate the suffix, falling back to file mtime,
            # so a collision-suffixed archive is never dropped from the listing.
            timestamp_str = archive_file.stem.replace("engine_", "")
            parts = timestamp_str.split("_")
            dt = None
            if len(parts) >= 2:
                try:
                    dt = datetime.strptime(f"{parts[0]}_{parts[1]}", "%Y%m%d_%H%M%S")
                except ValueError:
                    dt = None
            if dt is None:
                dt = datetime.fromtimestamp(archive_file.stat().st_mtime)

            # Size-guarded header read: only the top-level identity keys are
            # needed for the listing, so a giant legacy archived engine.json is
            # scanned head+tail rather than fully parsed (issue #243).
            data = _read_snapshot_header(archive_file)
            if not isinstance(data, dict):
                continue

            archived.append({
                "flow_id": data.get("flow_id", "unknown"),
                "status": data.get("status", "unknown"),
                "task_description": (data.get("task_description") or t("cli.common.no_description"))[:100],
                "archived_at": dt.isoformat(),
                "file": archive_file.name,
            })
        except (ValueError, json.JSONDecodeError, IOError):
            # Skip malformed archive files
            continue

    return archived


def _plan_decomposition_label(value: Any) -> str:
    """Localize one decomposition doctrine (WebUI ``plan.decomposition.*`` parity)."""
    text = str(value or "").strip()
    if text in ("capability", "granular"):
        return t(f"history.plan.decomposition.{text}")
    return text or t("history.plan.unknown")


def _plan_granularity_label(value: Any) -> str:
    """Localize one granularity tier (WebUI ``plan.granularity.*`` parity)."""
    text = str(value or "").strip()
    if text in ("auto", "single", "conservative"):
        return t(f"history.plan.granularity.{text}")
    return text or t("history.plan.unknown")


def _legacy_strategy_label(value: Any) -> str:
    """Localize a legacy flow's retired implementation-strategy value."""
    text = str(value or "").strip()
    if text in ("direct", "planned", "not_applicable"):
        return t(f"history.plan.legacy.{text}")
    return text or t("history.plan.unknown")


def _scope_mode_label(value: Any) -> str:
    """Localize one SELF_CHECK scope_mode (WebUI ``scope.mode.*`` parity)."""
    text = str(value or "").strip()
    if text in ("full", "incremental"):
        return t(f"history.scope.mode.{text}")
    return text or "-"


def _plan_group_count(step_details: List[Dict[str, Any]]) -> Optional[int]:
    """Return how many task groups the flow's PLAN step emitted, if known.

    ``task_groups`` wins over the recorded counter so a plan revision that
    rewrote the groups can never leave a stale count on display; ``None`` means
    PLAN has not run (or recorded nothing), never "one".
    """
    for step in step_details:
        if step.get("step_type") != "plan":
            continue
        outputs = step.get("outputs")
        if not isinstance(outputs, dict):
            continue
        groups = outputs.get("task_groups")
        if isinstance(groups, list):
            return len(groups)
        count = outputs.get("plan_group_count")
        if isinstance(count, int) and not isinstance(count, bool):
            return count
    return None


def _plan_mode_display_value(plan_mode: Dict[str, Any]) -> str:
    """Render the plan-mode row value, or "" when nothing is recoverable.

    A flow created under the retired strategy axis carries no doctrine of its
    own, so it is shown as what it actually recorded (``legacy_strategy``)
    rather than as a new-model value it never had.
    """
    decomposition = plan_mode.get("decomposition")
    if decomposition:
        value = t(
            "history.field.plan_mode_value",
            decomposition=_plan_decomposition_label(decomposition),
            granularity=_plan_granularity_label(plan_mode.get("granularity")),
        )
        count = plan_mode.get("group_count")
        if isinstance(count, int) and not isinstance(count, bool):
            value = t("history.field.plan_mode_groups", value=value, count=count)
        return value
    legacy = plan_mode.get("legacy_strategy")
    if legacy:
        value = _legacy_strategy_label(legacy)
        if legacy != "not_applicable":
            # "not applicable" names the absence of a PLAN -> IMPLEMENT segment,
            # which reads the same in both models — and both notes below
            # describe legacy provenance, so attaching them would date a
            # current small/review/survey flow to a model it never ran under.
            # Only a real recorded path (direct/planned) carries them.
            value = t("history.field.plan_mode_legacy", value=value)
            if plan_mode.get("inferred"):
                value = t("history.field.plan_mode_inferred", value=value)
        return value
    return ""


def _plan_mode_reason_text(plan_mode: Dict[str, Any]) -> str:
    """The plan-mode reason row text.

    A ``reason_key`` marks a sentence the projection itself authored (legacy
    inference / legacy strategy record), so it renders through the CLI catalog;
    a persisted reason is flow data recorded at decision time and is shown
    verbatim.
    """
    key = str(plan_mode.get("reason_key") or "").strip()
    if key in ("legacy_inference", "legacy_strategy", "no_plan_surface"):
        return t(f"history.field.plan_mode_reason_{key}")
    return str(plan_mode.get("reason") or "")


def _pricing_catalog(project_root: Path) -> Any:
    """Load the project's effective pricing catalog for cost estimation.

    A missing / invalid ``tianluo.yaml`` degrades to the built-in table rather
    than blocking history display; the estimate column then simply reflects
    built-in prices.
    """
    try:
        from ..config import load_pricing_catalog

        return load_pricing_catalog(project_root)
    except Exception:
        from ..pricing import PricingCatalog

        return PricingCatalog.builtin()


def _state_usage_payload(project_root: Path, flow: Any) -> Dict[str, Any]:
    """Build the usage/cost payload for a state-backed (active/archived) flow.

    The authoritative session record list wins for the flow totals; per-step
    records from ``step.outputs`` drive the per-step table.  A flow with no
    recoverable records whose legacy five-field tally is its only usage fact
    (``State.legacy_usage_ledger``, which stays true across a modern re-save)
    falls back to that adapted tally so old flows show *something* honest
    instead of a fabricated zero; a modern flow with an empty ledger has simply
    made no LLM call yet and reports no usage.
    """
    catalog = _pricing_catalog(project_root)
    records_by_step: Dict[str, List[UsageRecord]] = {}
    for step_id in flow.state.step_history:
        step = flow.state.steps.get(step_id)
        if not step or not isinstance(step.outputs, dict):
            continue
        raw_records = step.outputs.get("usage_records")
        if not isinstance(raw_records, list):
            continue
        records_by_step[step_id] = [
            UsageRecord.from_dict(raw) for raw in raw_records if isinstance(raw, dict)
        ]
    flow_records = list(flow.state.session_usage_records)
    if not flow_records:
        flow_records = [
            record for records in records_by_step.values() for record in records
        ]
    if not flow_records and getattr(flow.state, "legacy_usage_ledger", False):
        # No authoritative records AND the loaded engine.json's five-field tally
        # is its only usage fact (State.legacy_usage_ledger, shared test with
        # the daemon's flow_usage_summary): adapt it — an all-zero shape becomes
        # legacy_ambiguous — instead of silently omitting usage. A MODERN state
        # with an empty ledger means "zero LLM calls so far", never an unknown
        # call, so it must not synthesize one.
        flow_records = [
            legacy_usage_record(
                flow.state.session_token_usage.to_dict(),
                call_id="legacy-session-usage",
            )
        ]
    return build_usage_payload(
        records_by_step,
        catalog,
        flow_records=flow_records,
        call_id=str(flow.flow_id),
    )


def _detail_from_flow(project_root: Path, flow: Any) -> Dict[str, Any]:
    """Build detail dict from a FlowInstance object."""
    chat_sessions = get_flow_history(project_root, flow.flow_id)

    step_details = []
    for step_id in flow.state.step_history:
        step = flow.state.steps.get(step_id)
        if not step:
            continue
        step_details.append({
            "step_id": step.step_id,
            "step_type": step.step_type.value,
            "status": step.status.value,
            "started_at": step.started_at.isoformat() if step.started_at else None,
            "completed_at": step.completed_at.isoformat() if step.completed_at else None,
            "retry_count": step.retry_count,
            "error_message": step.error_message,
            "outputs": step.outputs,
        })

    completed, total = flow.get_progress()
    context = flow.state.context or {}

    return {
        "flow_id": flow.flow_id,
        "status": flow.status.value,
        "task_description": flow.task_description,
        "task_type": flow.task_type,
        "change_name": flow.change_name,
        "created_at": flow.created_at.isoformat(),
        "updated_at": flow.updated_at.isoformat(),
        "completed_at": flow.completed_at.isoformat() if flow.completed_at else None,
        "is_worktree_mode": flow.is_worktree_mode,
        "progress": {"completed": completed, "total": total},
        "current_step_id": flow.state.current_step_id,
        "steps": step_details,
        "chat_sessions": len(chat_sessions),
        # Control-plane projections: plan mode / scope audit / usage share one
        # backend with the daemon and server surfaces (see strategy_view.py /
        # usage.build_usage_payload), so CLI and WebUI never diverge.
        "plan_mode": plan_mode_view(
            context,
            task_type=flow.task_type,
            selected_steps=flow.state.selected_steps,
            plan_group_count=_plan_group_count(step_details),
        ),
        "review_scope": scope_view(context),
        "usage": _state_usage_payload(project_root, flow),
    }


def _load_archived_flow(project_root: Path, flow_id: str) -> Optional[Any]:
    """Try to load a FlowInstance from archive files matching flow_id.

    Delegates to the split-aware, size-guarded persistence loader: a new-format
    archive header has its externalized cold step payloads resolved from
    ``archive/steps/<flow_id>/`` (so ``luo history show`` reports each step's
    real outputs, not empty ones), while a giant legacy archive is read
    head+tail rather than fully parsed (issue #243 / #244 B5). A legacy archive
    that can only be read degraded returns ``None``, so the caller falls back to
    history-only detail rather than blocking on a 100 MB decode.
    """
    persistence = PersistenceManager(project_root)
    return persistence.load_archived_flow_by_id(flow_id)


def _detail_from_history(project_root: Path, flow_id: str) -> Optional[Dict[str, Any]]:
    """Build a minimal detail dict from history-only data."""
    history_dir = runtime_dir(project_root) / "history" / flow_id
    if not history_dir.is_dir():
        return None

    chat_sessions = get_flow_history(project_root, flow_id)

    # Try to read _meta.json for timestamps
    meta_path = history_dir / "_meta.json"
    created_at = ""
    task_type = None
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            created_at = meta.get("created_at", "")
            task_type = meta.get("type")
        except (json.JSONDecodeError, IOError):
            pass

    # Derive updated_at from latest file mtime
    try:
        latest_mtime = max(
            (f.stat().st_mtime for f in history_dir.iterdir() if f.is_file()),
            default=0,
        )
        updated_at = (
            datetime.fromtimestamp(latest_mtime).isoformat() if latest_mtime else ""
        )
    except Exception:
        updated_at = ""

    # Extract task description from chat history
    task_description = PersistenceManager.extract_history_summary(history_dir)

    # Build step list from chat session step IDs.
    # For history-only flows step.outputs are not preserved, but we can
    # reconstruct self_check pass numbering by counting consecutive self_check
    # sessions (resetting the counter at any non-self_check step).
    from ..config import resolve_self_check_passes_required

    try:
        # Use the EFFECTIVE pass count (derived from nested
        # ``llm_caller.steps.self_check`` chains when no explicit count is set),
        # not the raw ``workflow.self_check_passes_required`` (which stays at the
        # default 1 in the nested-derived case) — otherwise history-only flows
        # would render ``#i/1`` instead of ``#i/2``.
        passes_required = resolve_self_check_passes_required(project_root)
    except Exception:
        passes_required = None

    step_details = []
    sc_run_index = 0
    for session in chat_sessions:
        outputs = {}
        if session.step_type == "self_check":
            sc_run_index += 1
            outputs["self_check_pass_index"] = sc_run_index
            outputs["self_check_passes_required"] = (
                passes_required if passes_required is not None else sc_run_index
            )
        else:
            sc_run_index = 0
        step_details.append({
            "step_id": session.step_id,
            "step_type": session.step_type,
            "status": "completed",
            "started_at": None,
            "completed_at": None,
            "retry_count": 0,
            "error_message": None,
            "outputs": outputs,
        })

    # History-only flows carry no State, so plan mode / scope audit are not
    # recoverable; usage is rebuilt from each assistant message's records
    # (legacy five-field tallies adapt to flagged legacy_ambiguous records).
    records_by_step = collect_usage_records_from_sessions(chat_sessions)
    usage = build_usage_payload(
        records_by_step,
        _pricing_catalog(project_root),
        call_id=flow_id,
    )

    return {
        "flow_id": flow_id,
        "status": "history",
        "task_description": task_description,
        "task_type": task_type,
        "change_name": None,
        "created_at": created_at,
        "updated_at": updated_at,
        "completed_at": None,
        "is_worktree_mode": False,
        "progress": {"completed": len(step_details), "total": len(step_details)},
        "current_step_id": None,
        "steps": step_details,
        "chat_sessions": len(chat_sessions),
        "plan_mode": plan_mode_view(
            {},
            task_type=task_type,
            selected_steps=[step["step_type"] for step in step_details],
            plan_group_count=_plan_group_count(step_details),
        ),
        "review_scope": None,
        "usage": usage,
    }


def get_flow_detail(project_root: Path, flow_id: str) -> Optional[Dict[str, Any]]:
    """Get detailed information about a specific flow.

    Searches across three data sources in order:
    1. Active flow (tianluo/state/engine.json)
    2. Archived flows (tianluo/state/archive/engine_*.json)
    3. History-only flows (tianluo/history/{flow_id}/)
    """
    persistence = PersistenceManager(project_root)

    # 1. Active flow
    flow = persistence.load_flow()
    if flow and flow.flow_id == flow_id:
        return _detail_from_flow(project_root, flow)

    # 2. Archived flow
    archived_flow = _load_archived_flow(project_root, flow_id)
    if archived_flow:
        return _detail_from_flow(project_root, archived_flow)

    # 3. History-only flow
    return _detail_from_history(project_root, flow_id)


def _status_color(status: str) -> str:
    """Get color for status."""
    return {
        "completed": "green",
        "failed": "red",
        "running": "yellow",
        "init": "blue",
        "paused": "magenta",
    }.get(status.lower(), "white")


def _step_status_color(status: str) -> str:
    """Get color for step status."""
    return {
        "completed": "green",
        "failed": "red",
        "running": "yellow",
        "pending": "dim",
        "retrying": "magenta",
        "paused": "cyan",
    }.get(status.lower(), "white")


def _render_flows_table(flows: List[Dict[str, Any]], title: str) -> None:
    """Render a list of flows as a Rich table."""
    table = Table(title=title)
    table.add_column(t("history.col.flow_id"), style="cyan", no_wrap=True)
    table.add_column(t("history.col.status"))
    table.add_column(t("history.col.task_description"), style="white")
    table.add_column(t("history.col.progress"), justify="right")
    table.add_column(t("history.col.updated"), style="dim")
    table.add_column(t("history.col.source"), style="dim")

    status_colors = {
        "completed": "green",
        "failed": "red",
        "running": "yellow",
        "init": "blue",
        "paused": "magenta",
        "history": "dim",
    }

    for flow in flows:
        flow_id = flow.get("flow_id", "unknown")
        status = flow.get("status", "unknown")
        desc = flow.get("task_description") or t("cli.common.no_description")
        if len(desc) > 50:
            desc = desc[:50] + "..."
        progress = flow.get("progress", "-")
        updated = format_datetime(flow.get("updated_at", ""))
        source = flow.get("source", "")

        color = status_colors.get(status.lower(), "white")
        table.add_row(
            flow_id,
            f"[{color}]{t_status(status)}[/{color}]",
            desc,
            progress,
            updated,
            source,
        )

    console.print(table)
    typer.echo(t("history.list.show_hint"))


# Default command - list flows
@app.callback(invoke_without_command=True, help=t("cli.help.history.list.desc"))
def default_cmd(
    ctx: typer.Context,
    active_only: bool = typer.Option(False, "--active-only", help=t("cli.help.history.active_only")),
    archived_only: bool = typer.Option(False, "--archived-only", "-a", help=t("cli.help.history.archived_only")),
    json_output: bool = typer.Option(False, "--json", "-j", help=t("cli.help.common.json_output")),
):
    """List all flows (active, archived, and history)."""
    # If a subcommand is being invoked, skip this
    if ctx.invoked_subcommand is not None:
        return

    project_root = get_project_root()
    flows = list_all_flows(project_root)

    if active_only:
        flows = [f for f in flows if f.get("source") == "active"]
        title = t("history.title.active")
        empty_msg = t("history.empty.active")
    elif archived_only:
        flows = [f for f in flows if f.get("source") == "archived"]
        title = t("history.title.archived")
        empty_msg = t("history.empty.archived")
    else:
        title = t("history.title.all")
        empty_msg = t("history.empty.all")

    if json_output:
        typer.echo(json.dumps(flows, indent=2, default=str))
        return

    if not flows:
        typer.echo(empty_msg)
        return

    _render_flows_table(flows, title)


@app.command(name="list", help=t("cli.help.history.list.desc"))
def list_cmd(
    active_only: bool = typer.Option(False, "--active-only", help=t("cli.help.history.active_only")),
    archived_only: bool = typer.Option(False, "--archived-only", "-a", help=t("cli.help.history.archived_only")),
    json_output: bool = typer.Option(False, "--json", "-j", help=t("cli.help.common.json_output")),
):
    """List all flows (active, archived, and history)."""
    project_root = get_project_root()
    flows = list_all_flows(project_root)

    if active_only:
        flows = [f for f in flows if f.get("source") == "active"]
        title = t("history.title.active")
        empty_msg = t("history.empty.active")
    elif archived_only:
        flows = [f for f in flows if f.get("source") == "archived"]
        title = t("history.title.archived")
        empty_msg = t("history.empty.archived")
    else:
        title = t("history.title.all")
        empty_msg = t("history.empty.all")

    if json_output:
        typer.echo(json.dumps(flows, indent=2, default=str))
        return

    if not flows:
        typer.echo(empty_msg)
        return

    _render_flows_table(flows, title)


@app.command(name="show", help=t("cli.help.history.show.desc"))
def show_cmd(
    flow_id: str = typer.Argument(..., help=t("cli.help.history.show.flow_id")),
    json_output: bool = typer.Option(False, "--json", "-j", help=t("cli.help.common.json_output")),
    detailed: bool = typer.Option(False, "--detailed", "-d", help=t("cli.help.history.show.detailed")),
    verbose: bool = typer.Option(False, "--verbose", "-v", help=t("cli.help.history.show.verbose")),
):
    """Show detailed information about a specific flow."""
    # --verbose implies --detailed
    if verbose and not detailed:
        detailed = True

    project_root = get_project_root()

    detail = get_flow_detail(project_root, flow_id)

    if not detail:
        # Try to find partial matches across all sources
        all_flows = list_all_flows(project_root)
        matches = [f for f in all_flows if f.get("flow_id", "").startswith(flow_id)]

        if len(matches) == 1:
            detail = get_flow_detail(project_root, matches[0]["flow_id"])
        elif len(matches) > 1:
            typer.echo(t("history.multiple_match", flow_id=flow_id))
            for m in matches:
                typer.echo(
                    t(
                        "history.match_line",
                        flow_id=m.get("flow_id") or t("history.flow_id_unknown"),
                    )
                )
            raise typer.Exit(1)

    if not detail:
        typer.echo(t("history.not_found", flow_id=flow_id), err=True)
        raise typer.Exit(1)

    if json_output and not detailed:
        typer.echo(json.dumps(detail, indent=2, default=str))
        return

    if json_output and detailed:
        _show_detailed_json(project_root, detail)
        return

    # Display formatted details
    console.print(t("history.show.details_header", flow_id=detail['flow_id']))

    # Basic info table
    info_table = Table(show_header=False, box=None)
    info_table.add_column("Key", style="bold")
    info_table.add_column("Value")

    status_color = _status_color(detail['status'])
    info_table.add_row(
        t("history.field.status"),
        f"[{status_color}]{t_status(detail['status'])}[/{status_color}]",
    )
    info_table.add_row(t("history.field.task"), detail['task_description'])
    if detail.get('task_type'):
        info_table.add_row(t("history.field.type"), detail['task_type'])
    if detail.get('change_name'):
        info_table.add_row(t("history.field.change"), detail['change_name'])
    info_table.add_row(t("history.field.progress"), f"{detail['progress']['completed']}/{detail['progress']['total']}")
    info_table.add_row(t("history.field.created"), format_datetime(detail['created_at']))
    info_table.add_row(t("history.field.updated"), format_datetime(detail['updated_at']))
    if detail.get('completed_at'):
        info_table.add_row(t("history.field.completed"), format_datetime(detail['completed_at']))
    info_table.add_row(t("history.field.chat_sessions"), str(detail['chat_sessions']))

    # PLAN decomposition mode (doctrine / granularity / group count) — same
    # projection the daemon status and server payloads carry (strategy_view).
    plan_mode = detail.get("plan_mode")
    if isinstance(plan_mode, dict):
        value = _plan_mode_display_value(plan_mode)
        if value:
            info_table.add_row(t("history.field.plan_mode"), value)
            reason = _plan_mode_reason_text(plan_mode)
            if reason:
                info_table.add_row(t("history.field.plan_mode_reason"), reason)

    # SELF_CHECK scope audit (persisted round state).
    review_scope = detail.get("review_scope")
    if isinstance(review_scope, dict):
        scope_parts = []
        for key in ("active_round", "last_round"):
            round_data = review_scope.get(key)
            if isinstance(round_data, dict):
                scope_parts.append(
                    t(
                        "history.field.scope_round",
                        mode=_scope_mode_label(round_data.get("scope_mode")),
                        pass_index=round_data.get("pass_index") or "-",
                        fix=round_data.get("fix_iteration") or 0,
                    )
                )
                break
        if review_scope.get("completed_full_rounds"):
            scope_parts.append(
                t(
                    "history.field.scope_full_rounds",
                    count=review_scope["completed_full_rounds"],
                )
            )
        if scope_parts:
            info_table.add_row(t("history.field.scope"), ", ".join(scope_parts))

    console.print(info_table)

    # Steps table
    if detail['steps']:
        console.print(t("history.show.steps_header"))
        steps_table = Table()
        steps_table.add_column("#", justify="right")
        steps_table.add_column(t("history.col.step_type"))
        steps_table.add_column(t("history.col.status"))
        steps_table.add_column(t("history.col.retries"), justify="right")
        steps_table.add_column(t("cli.common.error"), style="red")

        for i, step in enumerate(detail['steps'], 1):
            status_color = _step_status_color(step['status'])
            error_msg = step.get('error_message', '') or ""
            if error_msg and len(error_msg) > 40:
                error_msg = error_msg[:40] + "..."

            # Surface self_check pass numbering (#i/N) when available
            step_label = step['step_type']
            outputs = step.get('outputs', {})
            if step['step_type'] == 'self_check':
                pass_index = outputs.get('self_check_pass_index')
                passes_required = outputs.get('self_check_passes_required')
                if pass_index is not None and passes_required is not None:
                    step_label = f"self_check #{pass_index}/{passes_required}"

            steps_table.add_row(
                str(i),
                step_label,
                f"[{status_color}]{t_status(step['status'])}[/{status_color}]",
                str(step.get('retry_count', 0)),
                error_msg,
            )
        console.print(steps_table)

    # Legacy plan artifacts remain visible in their historical section even
    # though task_groups / adjudicated_plan no longer carry SELF_CHECK
    # authority: they are scheduling/history data, still worth displaying.
    _show_plan_artifacts(detail)

    # Independent usage/cost section, fed by the same structured payload the
    # --json output carries (build_usage_payload).
    usage_payload = detail.get("usage")
    console.print(t("history.usage.header"))
    if isinstance(usage_payload, dict) and (usage_payload.get("calls") or usage_payload.get("steps")):
        for renderable in build_history_usage_renderables(
            usage_payload, _pricing_catalog(project_root)
        ):
            console.print(renderable)
    else:
        console.print(t("history.usage.no_usage"))

    # Detailed LLM call display
    if detailed:
        _show_detailed_sessions(project_root, detail['flow_id'], verbose=verbose)

    console.print(t("history.show.restore_hint", flow_id=detail['flow_id']))


def _show_plan_artifacts(detail: Dict[str, Any]) -> None:
    """Render legacy PLAN / adjudication artifacts from step outputs.

    Scans the flow's step outputs for the three historical data shapes the
    modern SELF_CHECK no longer treats as authority — PLAN task_groups,
    adjudicated_plan, and findings whose expectation_source is ``plan_task`` —
    and renders a compact summary so they remain inspectable in their
    historical section without implying any acceptance weight.
    """
    rows: List[tuple] = []
    plan_step = None
    for step in detail.get("steps", []):
        outputs = step.get("outputs") or {}
        if step.get("step_type") == "plan" and isinstance(
            outputs.get("task_groups"), list
        ):
            groups = outputs["task_groups"]
            if groups:
                plan_step = step
        if outputs.get("adjudicated_plan"):
            rows.append((t("history.plan_artifacts.adjudicated_plan"), "plan"))
        if step.get("step_type") == "self_check":
            for issue in outputs.get("issues") or []:
                source = issue.get("expectation_source") if isinstance(issue, dict) else None
                if isinstance(source, dict) and source.get("type") == "plan_task":
                    rows.append((t("history.plan_artifacts.plan_task_finding"), "self_check"))
    if plan_step:
        groups = plan_step["outputs"]["task_groups"]
        rows.append(
            (
                t("history.plan_artifacts.task_groups", count=len(groups)),
                "plan",
            )
        )
    if not rows:
        return

    console.print(t("history.plan_artifacts.header"))
    artifacts_table = Table(show_header=False, box=None)
    artifacts_table.add_column(t("history.plan_artifacts.artifact"), style="bold")
    artifacts_table.add_column(t("history.plan_artifacts.step"))
    for artifact, step_type in rows:
        artifacts_table.add_row(artifact, step_type)
    console.print(artifacts_table)


def _show_detailed_sessions(
    project_root: Path, flow_id: str, verbose: bool = False
) -> None:
    """Render detailed LLM call sessions for a flow."""
    from rich.rule import Rule

    sessions = get_flow_history(project_root, flow_id)
    if not sessions:
        console.print(t("history.detail.no_chat_history"))
        return

    sessions = interleave_sessions_for_display(sessions)

    console.print(t("history.detail.llm_calls_header"))

    for session in sessions:
        console.print(Rule(
            t("history.detail.session_rule", step_type=session.step_type, step_id=session.step_id),
            style="cyan",
        ))
        renderables = render_session_detailed(session, verbose=verbose)
        for r in renderables:
            console.print(r)


def _show_detailed_json(project_root: Path, detail: dict) -> None:
    """Output detailed flow info with chat history as JSON."""
    flow_id = detail["flow_id"]
    output = {**detail, "chat_history": get_detailed_json(project_root, flow_id)}
    typer.echo(json.dumps(output, indent=2, default=str))


@app.command(name="restore", help=t("cli.help.history.restore.desc"))
def restore_cmd(
    flow_id: str = typer.Argument(..., help=t("cli.help.history.restore.flow_id")),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help=t("cli.help.history.restore.dry_run")),
):
    """Restore a previous session by resuming the flow.

    This delegates to 'luo run --resume --flow-id <flow_id>'.
    """
    project_root = get_project_root()

    # Validate flow exists across all sources (active, archived, history)
    all_flows = list_all_flows(project_root)
    exact = [f for f in all_flows if f.get("flow_id") == flow_id]

    if not exact:
        # Try to find by prefix
        matches = [f for f in all_flows if f.get("flow_id", "").startswith(flow_id)]

        if len(matches) == 1:
            flow_id = matches[0]["flow_id"]
        elif len(matches) > 1:
            typer.echo(t("history.multiple_match", flow_id=flow_id))
            for m in matches:
                typer.echo(
                    t(
                        "history.match_line",
                        flow_id=m.get("flow_id") or t("history.flow_id_unknown"),
                    )
                )
            raise typer.Exit(1)
        else:
            typer.echo(t("history.not_found", flow_id=flow_id), err=True)
            raise typer.Exit(1)

    if dry_run:
        typer.echo(t("history.restore.would_restore", flow_id=flow_id))
        typer.echo(t("history.restore.command", flow_id=flow_id))
        return

    # Delegate to luo run --resume
    typer.echo(t("history.restore.restoring", flow_id=flow_id))
    result = subprocess.run(
        [sys.executable, "-m", "tianluo", "run", "--resume", "--flow-id", flow_id],
        cwd=project_root,
    )
    raise typer.Exit(result.returncode)


@app.command(name="archived", help=t("cli.help.history.archived.desc"))
def archived_cmd(
    json_output: bool = typer.Option(False, "--json", "-j", help=t("cli.help.common.json_output")),
):
    """List all archived flows."""
    project_root = get_project_root()

    archived = list_archived_flows_from_disk(project_root)

    if json_output:
        typer.echo(json.dumps(archived, indent=2, default=str))
        return

    if not archived:
        typer.echo(t("history.empty.archived"))
        return

    table = Table(title=t("history.title.archived"))
    table.add_column(t("history.col.flow_id"), style="cyan", no_wrap=True)
    table.add_column(t("history.col.status"), style="green")
    table.add_column(t("history.col.task_description"), style="white")
    table.add_column(t("history.col.archived_at"), style="dim")

    for flow in archived:
        flow_id = flow["flow_id"]
        status = flow["status"]
        desc = flow["task_description"]
        if len(desc) > 50:
            desc = desc[:50] + "..."
        archived_at = format_datetime(flow["archived_at"])

        status_style = {
            "completed": "green",
            "failed": "red",
            "running": "yellow",
        }.get(status.lower(), "white")

        table.add_row(
            flow_id,
            f"[{status_style}]{t_status(status)}[/{status_style}]",
            desc,
            archived_at,
        )

    console.print(table)


if __name__ == "__main__":
    app()
