"""Shared usage/cost backend access for the daemon's polling surfaces.

The daemon never implements its own pricing or token-sum formulas: it loads
the project's effective catalog through :func:`tianluo.config.load_pricing_catalog`
and aggregates through :mod:`tianluo.usage` — the same backend the CLI, the
server and the WebUI consume.  This module only adds the per-root catalog
cache the 2-second snapshot / index paths need, so the tianluo.yaml pricing
section is re-parsed on change, not on every poll.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_catalogs: Dict[str, Tuple[float, Any]] = {}
_lock = threading.Lock()


def project_pricing_catalog(project_root) -> Any:
    """Return the project's effective pricing catalog, cached by yaml mtime.

    A missing / invalid project configuration degrades to the built-in price
    table — the estimate column then reflects built-in prices rather than
    blocking the status snapshot.
    """
    try:
        root = Path(project_root)
    except (TypeError, ValueError):
        return _builtin()
    try:
        mtime = (root / "tianluo.yaml").stat().st_mtime
    except OSError:
        mtime = 0.0
    key = str(root)
    cached = _catalogs.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        from ..config import load_pricing_catalog

        catalog = load_pricing_catalog(root)
    except Exception:
        catalog = _builtin()
    with _lock:
        _catalogs[key] = (mtime, catalog)
    return catalog


def _builtin() -> Any:
    from ..pricing import PricingCatalog

    return PricingCatalog.builtin()


def _resolve_steps_roots(
    project_root: Any, partition: Any, flow_id: str
) -> List[Path]:
    """The cold-file partition dirs a header's ``cold_ref`` entries resolve to.

    Mirrors the persistence layer's resolution (``steps/<flow_id>/`` under the
    runtime state dir, honoring a recorded ``cold_partition`` override) AND —
    like :func:`tianluo.strategy_view.resolve_flow_context` — the archived copy
    under ``archive/steps/``: ``clear_state`` archives cold files there and
    later prunes the live partition, so an archived flow's per-step usage is
    only recoverable from the archive. Without it the daemon degrades to the
    legacy tally while the archive-aware CLI shows the real per-call records.
    Empty when the root is unusable, so callers skip cold reads instead of
    touching a relative path.
    """
    if project_root is None:
        return []
    try:
        from ..runtime_paths import runtime_dir

        state_dir = runtime_dir(Path(project_root)) / "state"
    except (TypeError, ValueError, OSError):
        return []
    leaf = str(partition) if partition else str(flow_id)
    return [
        state_dir / "steps" / leaf,
        state_dir / "archive" / "steps" / leaf,
    ]


def _per_step_usage_records(
    state: Dict[str, Any],
    project_root: Any,
    flow_id: str,
) -> List[Any]:
    """Recover per-step records from ``step.outputs`` (CLI-parity fallback).

    The CLI history path falls back to the union of
    ``step.outputs.usage_records`` when the session ledger is empty; the
    daemon reads the same on-disk state, so it must apply the same fallback
    or the WebUI omits usage the CLI shows. Legacy fully-inline states carry
    ``outputs`` directly on the step entry; new-format headers externalize
    the body to a cold file referenced by ``cold_ref`` (``steps/<flow_id>/``
    under the runtime state dir), resolved the same way the persistence
    layer does. Unreadable or malformed sources are skipped — this is a
    best-effort recovery path, never a hard error on the snapshot hot path.
    """
    from ..usage import UsageRecord

    steps = state.get("steps")
    if not isinstance(steps, dict):
        return []
    partition = state.get("cold_partition")
    steps_roots = _resolve_steps_roots(project_root, partition, flow_id)
    records: List[Any] = []
    for entry in steps.values():
        if not isinstance(entry, dict):
            continue
        outputs = entry.get("outputs")
        if not isinstance(outputs, dict):
            cold_ref = entry.get("cold_ref")
            if isinstance(cold_ref, dict) and cold_ref.get("file"):
                for steps_root in steps_roots:
                    try:
                        cold = json.loads(
                            (steps_root / str(cold_ref["file"])).read_text(
                                encoding="utf-8"
                            )
                        )
                    except (OSError, ValueError, TypeError):
                        continue
                    if isinstance(cold, dict):
                        outputs = cold.get("outputs")
                        break
        if not isinstance(outputs, dict):
            continue
        raw_records = outputs.get("usage_records")
        if not isinstance(raw_records, list):
            continue
        for raw in raw_records:
            if isinstance(raw, dict):
                records.append(UsageRecord.from_dict(raw))
    return records


def flow_usage_summary(state: Any, *, project_root=None, call_id: str = "flow", flow_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Build the compact wire usage summary for one engine-shaped ``state``.

    ``state`` is the raw ``engine.json`` ``state`` object (a dict).  The
    authoritative ``session_usage_records`` list drives the totals; a state
    whose five-field session tally is its ONLY usage fact (see
    :func:`~tianluo.usage.legacy_session_tally_is_authoritative` — true even
    after the modern serializer re-saves such a flow with an empty ledger)
    adapts through :func:`~tianluo.usage.legacy_usage_record`.
    The fallback chain mirrors the CLI history path exactly — session ledger,
    else the union of ``step.outputs.usage_records``, else the legacy tally —
    so both surfaces report identical usage for the same flow.  Returns
    ``None`` when the state records no usage at all — callers then omit the
    field instead of emitting a misleading zero summary.
    """
    from ..usage import (
        UsageRecord,
        UsageSummary,
        legacy_session_tally_is_authoritative,
        legacy_usage_record,
    )

    if not isinstance(state, dict):
        return None
    raw_records = state.get("session_usage_records")
    if isinstance(raw_records, list):
        records = [
            UsageRecord.from_dict(raw) for raw in raw_records if isinstance(raw, dict)
        ]
    else:
        records = []
    if not records:
        records = _per_step_usage_records(
            state, project_root, str(flow_id or call_id)
        )
    if not records and legacy_session_tally_is_authoritative(state):
        # Only a state whose legacy tally is its ONLY usage fact may adapt it.
        # That test is shared with State.from_dict (which drives the history
        # CLI's ``legacy_usage_ledger`` flag) so the two surfaces can never
        # disagree about the same file: a non-zero tally beside an empty ledger
        # is legacy data even after a modern re-save, while an all-zero tally
        # under a present ledger key is a modern flow that has simply not made
        # its first LLM call.
        records = [
            legacy_usage_record(
                state.get("session_token_usage"), call_id=f"legacy:{call_id}"
            )
        ]
    if not records:
        return None
    catalog = (
        project_pricing_catalog(project_root) if project_root else _builtin()
    )
    summary = UsageSummary.summarize(records, catalog, call_id=call_id)
    return summary.to_dict_for_wire()
