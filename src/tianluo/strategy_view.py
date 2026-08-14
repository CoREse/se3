"""Read-only implementation-strategy projection for control-plane surfaces.

The engine resolves and persists strategy decisions through
:mod:`tianluo.engine.implementation_strategy`; the daemon deliberately does
not import the engine (it parses raw ``engine.json`` dicts), so this
stdlib-only module exposes the one string-based projection shared by
``luo history show``, the daemon status snapshot / session metadata and the
server API.  It is display-only: it never writes back to persisted state, and
a legacy flow's inferred view never rewrites the recorded path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

REQUESTED_IMPLEMENTATION_STRATEGY_KEY = "requested_implementation_strategy"
EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY = "effective_implementation_strategy"
IMPLEMENTATION_STRATEGY_REASON_KEY = "strategy_reason"

VALID_REQUESTED = frozenset({"auto", "direct", "planned"})
VALID_EFFECTIVE = frozenset({"direct", "planned", "not_applicable"})

#: Task types whose own table entry contains a PLAN -> IMPLEMENT segment.
CHOICE_SURFACE_TASK_TYPES = frozenset({"feature", "bugfix", "discovery"})
#: Task types that never carry a PLAN -> IMPLEMENT strategy surface.
NO_SURFACE_TASK_TYPES = frozenset({"small", "review", "survey"})

LEGACY_INFER_REASON = "Inferred from persisted legacy task type and selected_steps."
#: Machine-readable companion to :data:`LEGACY_INFER_REASON`. WHY: the sentence
#: itself is authored HERE, at projection time, so it is UI chrome rather than
#: persisted flow data — surfaces must render it through their own i18n
#: catalog. ``reason`` keeps the English text for older clients that only know
#: that field.
LEGACY_INFER_REASON_KEY = "legacy_inference"



def has_choice_surface(task_type: Optional[str]) -> bool:
    """Mirror of the engine's ``ImplementationStrategyResolver.has_choice_surface``.

    WHY: the engine derives applicability from ``get_default_step_sequence``,
    whose unknown-type fallback is the feature sequence — so a retired task
    type persisted in an old flow *does* carry a PLAN -> IMPLEMENT surface.
    Matching that fallback here (rather than testing the explicit table
    entries) keeps the daemon/CLI-history projection and the engine projection
    from reporting two different strategies for the same legacy flow.
    """
    task_type = _optional_str(task_type)
    if not task_type or task_type == "pending":
        return False
    return task_type not in NO_SURFACE_TASK_TYPES


def _optional_str(value: Any) -> Optional[str]:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def resolve_flow_context(
    state: Optional[Mapping[str, Any]],
    *,
    state_dir: Optional[Path] = None,
    flow_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a flow's ``State.context``, following an externalized reference.

    WHY: the hot/cold persistence layer pops ``context`` out of the
    ``engine.json`` header and writes it to ``steps/<flow_id>/_context.json``,
    leaving only a ``context_ref`` behind — so a control-plane surface that
    reads ``state["context"]`` alone sees an empty context for *every*
    current-format flow and falls back to legacy inference, fabricating a
    strategy the flow never requested.  Reading the referenced cold file here
    keeps the daemon/server projections on the persisted values without
    importing the engine.  Unreadable/absent cold files degrade to the inline
    value (possibly ``{}``), matching the persistence layer's own tolerance.
    """
    if not isinstance(state, Mapping):
        return {}
    inline = state.get("context")
    inline_context: Dict[str, Any] = dict(inline) if isinstance(inline, dict) else {}
    if inline_context:
        return inline_context

    ref = state.get("context_ref")
    if not isinstance(ref, Mapping):
        return inline_context
    filename = ref.get("file")
    if not isinstance(filename, str) or not filename:
        return inline_context
    if state_dir is None:
        return inline_context

    partition = state.get("cold_partition") or flow_id
    if not partition:
        return inline_context
    # Live flows keep their cold files under state/steps/; an archived header
    # resolves against the copy archived alongside it.
    candidates = (
        Path(state_dir) / "steps" / str(partition) / filename,
        Path(state_dir) / "archive" / "steps" / str(partition) / filename,
    )
    for candidate in candidates:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            resolved = payload.get("context")
            if isinstance(resolved, dict):
                return dict(resolved)
    return inline_context


def strategy_view(
    context: Optional[Mapping[str, Any]],
    *,
    task_type: Optional[str],
    selected_steps: Sequence[Any],
) -> Dict[str, Any]:
    """Return the display projection of a flow's implementation strategy.

    ``context`` is ``State.context`` (or ``{}`` when unavailable — e.g. a
    degraded header read); ``selected_steps`` is the persisted step list.
    The result is always JSON-safe::

        {
            "requested": "auto" | "direct" | "planned" | None,
            "effective": "direct" | "planned" | "not_applicable" | None,
            "reason": str,
            "inferred": bool,  # True when derived from legacy persisted steps
        }

    Persisted values win; a legacy flow without them is inferred from its
    recorded ``task_type`` + ``selected_steps`` (PLAN present -> planned,
    small/review/survey -> not_applicable, a choice-surface flow with
    IMPLEMENT but no PLAN -> direct).  ``requested`` stays ``None`` only when
    nothing at all can be recovered (empty context and empty steps).
    """
    context = context or {}
    requested = _optional_str(context.get(REQUESTED_IMPLEMENTATION_STRATEGY_KEY))
    if requested is not None and requested in VALID_REQUESTED:
        effective = _optional_str(
            context.get(EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY)
        )
        if effective is not None and effective not in VALID_EFFECTIVE:
            effective = None
        return {
            "requested": requested,
            "effective": effective,
            "reason": _optional_str(
                context.get(IMPLEMENTATION_STRATEGY_REASON_KEY)
            )
            or "",
            # Persisted reasons are flow data recorded at decision time, not
            # projection chrome: rendered verbatim, so no key.
            "reason_key": "",
            "inferred": False,
        }

    steps = {
        step.value if not isinstance(step, str) else step
        for step in selected_steps
    }
    task_type = _optional_str(task_type) or ""
    if not steps:
        # Nothing on disk to infer from (degraded header read); a no-surface
        # task type is still determinable, anything else stays unknown.
        if task_type in NO_SURFACE_TASK_TYPES:
            return {
                "requested": "planned",
                "effective": "not_applicable",
                "reason": LEGACY_INFER_REASON,
                "reason_key": LEGACY_INFER_REASON_KEY,
                "inferred": True,
            }
        return {
            "requested": None,
            "effective": None,
            "reason": "",
            "reason_key": "",
            "inferred": False,
        }
    if task_type in NO_SURFACE_TASK_TYPES:
        effective: Optional[str] = "not_applicable"
        requested_for_display: Optional[str] = "planned"
    elif "plan" in steps and "implement" in steps:
        effective = "planned"
        requested_for_display = "planned"
    elif has_choice_surface(task_type) and "implement" in steps:
        effective = "direct"
        requested_for_display = "direct"
    else:
        effective = "not_applicable"
        requested_for_display = "planned"
    return {
        "requested": requested_for_display,
        "effective": effective,
        "reason": LEGACY_INFER_REASON if effective is not None else "",
        "reason_key": LEGACY_INFER_REASON_KEY if effective is not None else "",
        "inferred": effective is not None,
    }


def scope_view(context: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the persisted SELF_CHECK scope audit, or ``None`` when absent.

    Reads the engine's ``self_check_review`` round controller state (see
    :mod:`tianluo.engine.review_scope`) without importing the engine: the
    active round, the last finished round, and the completed-full-round count
    are the audit facts control-plane surfaces display.
    """
    context = context or {}
    raw = context.get("self_check_review")
    if not isinstance(raw, dict):
        return None
    view: Dict[str, Any] = {}
    for key in ("active_round", "last_round", "last_clean_full_round_id"):
        value = raw.get(key)
        view[key] = dict(value) if isinstance(value, dict) else value
    view["completed_full_rounds"] = int(raw.get("completed_full_rounds", 0) or 0)
    if not any(
        key in view and view[key] not in (None, "", {})
        for key in ("active_round", "last_round", "last_clean_full_round_id")
    ):
        return None
    return view
