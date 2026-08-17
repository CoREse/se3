"""Read-only plan-mode projection for control-plane surfaces.

The engine resolves and persists the PLAN decomposition decision through
:mod:`tianluo.engine.plan_decomposition`; the daemon deliberately does not
import the engine (it parses raw ``engine.json`` dicts), so this stdlib-only
module exposes the one string-based projection shared by ``luo history show``,
the daemon status snapshot / session metadata and the server API.  It is
display-only: it never writes back to persisted state, and a legacy flow's
view never rewrites the recorded path.

WHY the module keeps its ``strategy_view`` filename while projecting the new
model: the retired ``implementation_strategy`` axis and the plan-mode keys
occupy the same slot on every control-plane surface — one read-only projection
of "what execution shape did this flow enter". Renaming the file would churn
six import sites for no behavioural gain, and the module's real contract (a
stdlib-only, engine-free, write-free projection) is unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

PLAN_DECOMPOSITION_KEY = "plan_decomposition"
PLAN_GRANULARITY_KEY = "plan_granularity"
PLAN_MODE_REASON_KEY = "plan_mode_reason"
PLAN_GROUP_COUNT_KEY = "plan_group_count"

#: Context key written by flows created before the plan-decomposition model.
#: Read-only here — see :func:`plan_mode_view`.
LEGACY_EFFECTIVE_STRATEGY_KEY = "effective_implementation_strategy"

VALID_DECOMPOSITIONS = frozenset({"capability", "granular"})
VALID_GRANULARITIES = frozenset({"auto", "single", "conservative"})
#: Mirrors :class:`tianluo.engine.plan_decomposition.PlanModeResolver`'s own
#: default so a flow that persisted the doctrine but not the granularity (an
#: early-format write) still projects the granularity it actually ran under.
DEFAULT_GRANULARITY = "auto"

#: Values the retired axis could record in ``effective_implementation_strategy``.
VALID_LEGACY_STRATEGIES = frozenset({"direct", "planned", "not_applicable"})

#: Task types whose own table entry contains a PLAN -> IMPLEMENT segment.
CHOICE_SURFACE_TASK_TYPES = frozenset({"feature", "bugfix", "discovery"})
#: Task types that carry no PLAN -> IMPLEMENT surface at all, in either model.
NO_SURFACE_TASK_TYPES = frozenset({"small", "review", "survey"})

LEGACY_INFER_REASON = "Inferred from persisted legacy task type and selected_steps."
#: Machine-readable companion to :data:`LEGACY_INFER_REASON`. WHY: the sentence
#: itself is authored HERE, at projection time, so it is UI chrome rather than
#: persisted flow data — surfaces must render it through their own i18n
#: catalog. ``reason`` keeps the English text for older clients that only know
#: that field.
LEGACY_INFER_REASON_KEY = "legacy_inference"

LEGACY_STRATEGY_REASON = (
    "Flow ran under the retired implementation-strategy model; "
    "showing the path it recorded."
)
#: Companion key for :data:`LEGACY_STRATEGY_REASON` (same UI-chrome rule).
LEGACY_STRATEGY_REASON_KEY = "legacy_strategy"


def has_choice_surface(task_type: Optional[str]) -> bool:
    """Whether *task_type*'s step table ever contained a PLAN -> IMPLEMENT segment.

    WHY this outlives the retired strategy axis: legacy-flow inference still
    needs it, and the engine derives applicability from
    ``get_default_step_sequence``, whose unknown-type fallback is the feature
    sequence — so a retired task type persisted in an old flow *does* carry the
    surface. Matching that fallback here (rather than testing the explicit
    table entries) keeps the daemon/CLI-history projection and the engine
    projection from describing the same legacy flow two different ways.
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


def _optional_int(value: Any) -> Optional[int]:
    # bool is an int subclass; a True here would render as a group count of 1.
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


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
    current-format flow and falls back to legacy inference, describing a
    current flow as if it predated the plan-mode model.  Reading the cold file
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


#: Group counts already recovered from a cold PLAN payload, keyed by that
#: payload's content hash. WHY a cache at all: the daemon recomputes every
#: flow's projection on each poll, and a PLAN step's cold payload carries the
#: whole plan document — re-reading it per poll is exactly the read storm the
#: hot/cold split was introduced to remove. Keying on the persisted content
#: hash makes the read happen once per plan *revision* instead, and makes a
#: stale count impossible: a rewritten plan gets a new hash, hence a new read.
_COLD_GROUP_COUNT_CACHE: Dict[str, Optional[int]] = {}
#: Cleared wholesale past this size. A daemon watches a bounded set of projects,
#: so the map stays tiny in practice; the cap only guarantees an unbounded run
#: of plan revisions cannot grow it without limit, and a full clear costs at
#: most one re-read per live flow.
_COLD_GROUP_COUNT_CACHE_MAX = 256


def plan_group_count_from_state(
    state: Optional[Mapping[str, Any]],
    *,
    state_dir: Optional[Path] = None,
    flow_id: Optional[str] = None,
) -> Optional[int]:
    """Return how many task groups the flow's PLAN step emitted, if knowable.

    Reads an inline PLAN body directly; for a hot/cold flow (whose step bodies
    are externalized) it follows the recorded ``cold_ref``, memoized on that
    reference's content hash.  ``None`` means "unknown" — never "one".
    """
    if not isinstance(state, Mapping):
        return None
    steps = state.get("steps")
    if not isinstance(steps, Mapping):
        return None
    for step in steps.values():
        if not isinstance(step, Mapping) or step.get("step_type") != "plan":
            continue
        count = _group_count_from_outputs(step.get("outputs"))
        if count is not None:
            return count
        count = _group_count_from_cold_ref(
            step.get("cold_ref"),
            partition=state.get("cold_partition") or flow_id,
            state_dir=state_dir,
        )
        if count is not None:
            return count
    return None


def _group_count_from_outputs(outputs: Any) -> Optional[int]:
    """Count the groups in a PLAN step's outputs mapping.

    ``task_groups`` wins over the recorded counter so a plan revision that
    rewrote the groups can never leave a stale count on display.
    """
    if not isinstance(outputs, Mapping):
        return None
    groups = outputs.get("task_groups")
    if isinstance(groups, list):
        return len(groups)
    return _optional_int(outputs.get(PLAN_GROUP_COUNT_KEY))


def _group_count_from_cold_ref(
    cold_ref: Any,
    *,
    partition: Any,
    state_dir: Optional[Path],
) -> Optional[int]:
    """Resolve a PLAN step's externalized body far enough to count its groups."""
    if not isinstance(cold_ref, Mapping) or state_dir is None or not partition:
        return None
    filename = cold_ref.get("file")
    content_hash = cold_ref.get("hash")
    if not isinstance(filename, str) or not filename:
        return None
    if not isinstance(content_hash, str) or not content_hash:
        return None
    if content_hash in _COLD_GROUP_COUNT_CACHE:
        return _COLD_GROUP_COUNT_CACHE[content_hash]

    # Same live/archived candidate pair as resolve_flow_context.
    count: Optional[int] = None
    for candidate in (
        Path(state_dir) / "steps" / str(partition) / filename,
        Path(state_dir) / "archive" / "steps" / str(partition) / filename,
    ):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            count = _group_count_from_outputs(payload.get("outputs"))
            break
    else:
        # No candidate was readable: a transient blip, not a settled answer —
        # do not memoize it, or one bad poll would pin "unknown" for the life
        # of this plan revision.
        return None
    if len(_COLD_GROUP_COUNT_CACHE) >= _COLD_GROUP_COUNT_CACHE_MAX:
        _COLD_GROUP_COUNT_CACHE.clear()
    _COLD_GROUP_COUNT_CACHE[content_hash] = count
    return count


def plan_mode_view(
    context: Optional[Mapping[str, Any]],
    *,
    task_type: Optional[str],
    selected_steps: Sequence[Any],
    plan_group_count: Optional[int] = None,
) -> Dict[str, Any]:
    """Return the display projection of a flow's PLAN decomposition mode.

    ``context`` is ``State.context`` (or ``{}`` when unavailable — e.g. a
    degraded header read); ``selected_steps`` is the persisted step list.
    The result is always JSON-safe::

        {
            "decomposition": "capability" | "granular" | None,
            "granularity": "auto" | "single" | "conservative" | None,
            "group_count": int | None,      # None until PLAN has run
            "reason": str,
            "reason_key": str,
            "legacy_strategy": "direct" | "planned" | "not_applicable" | None,
            "inferred": bool,
        }

    WHY a legacy flow projects ``None`` for the new fields instead of a mapped
    equivalent: a flow created under the retired strategy axis never made a
    decomposition decision, and rendering one as if it had would present a
    fabricated value as recorded state.  Such a flow is described by
    ``legacy_strategy`` alone — recovered from its persisted
    ``effective_implementation_strategy``, or, failing that, inferred from its
    recorded ``task_type`` + ``selected_steps`` (PLAN present -> planned,
    small/review/survey -> not_applicable, a choice-surface flow with IMPLEMENT
    but no PLAN -> direct).  Everything stays ``None`` only when nothing at all
    can be recovered (empty context and empty steps).
    """
    context = context or {}
    group_count = _optional_int(plan_group_count)
    if group_count is None:
        group_count = _optional_int(context.get(PLAN_GROUP_COUNT_KEY))

    decomposition = _optional_str(context.get(PLAN_DECOMPOSITION_KEY))
    if decomposition in VALID_DECOMPOSITIONS:
        granularity = _optional_str(context.get(PLAN_GRANULARITY_KEY))
        if granularity not in VALID_GRANULARITIES:
            granularity = DEFAULT_GRANULARITY
        return {
            "decomposition": decomposition,
            "granularity": granularity,
            "group_count": group_count,
            "reason": _optional_str(context.get(PLAN_MODE_REASON_KEY)) or "",
            # Persisted reasons are flow data recorded at decision time, not
            # projection chrome: rendered verbatim, so no key.
            "reason_key": "",
            "legacy_strategy": None,
            "inferred": False,
        }

    legacy = _optional_str(context.get(LEGACY_EFFECTIVE_STRATEGY_KEY))
    if legacy in VALID_LEGACY_STRATEGIES:
        return _legacy_projection(
            legacy, group_count, LEGACY_STRATEGY_REASON, LEGACY_STRATEGY_REASON_KEY
        )

    steps = {
        step.value if not isinstance(step, str) else step
        for step in selected_steps
    }
    task_type = _optional_str(task_type) or ""
    if not steps:
        # Nothing on disk to infer from (degraded header read); a no-surface
        # task type is still determinable, anything else stays unknown.
        if task_type in NO_SURFACE_TASK_TYPES:
            return _legacy_projection(
                "not_applicable",
                group_count,
                LEGACY_INFER_REASON,
                LEGACY_INFER_REASON_KEY,
            )
        return {
            "decomposition": None,
            "granularity": None,
            "group_count": group_count,
            "reason": "",
            "reason_key": "",
            "legacy_strategy": None,
            "inferred": False,
        }
    if task_type in NO_SURFACE_TASK_TYPES:
        inferred_strategy = "not_applicable"
    elif "plan" in steps and "implement" in steps:
        inferred_strategy = "planned"
    elif has_choice_surface(task_type) and "implement" in steps:
        inferred_strategy = "direct"
    else:
        inferred_strategy = "not_applicable"
    return _legacy_projection(
        inferred_strategy, group_count, LEGACY_INFER_REASON, LEGACY_INFER_REASON_KEY
    )


def _legacy_projection(
    legacy_strategy: str,
    group_count: Optional[int],
    reason: str,
    reason_key: str,
) -> Dict[str, Any]:
    """Shape a projection for a flow that predates the plan-decomposition model."""
    return {
        "decomposition": None,
        "granularity": None,
        "group_count": group_count,
        "reason": reason,
        "reason_key": reason_key,
        "legacy_strategy": legacy_strategy,
        "inferred": True,
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
