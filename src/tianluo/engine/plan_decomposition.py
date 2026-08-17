"""PLAN decomposition doctrine + granularity: data contract and persistence.

Replaces the retired ``implementation_strategy`` routing axis. The two are NOT
the same shape of thing, and the difference is the whole point of this module:

WHY this module owns no step-sequence transform: the old strategy axis decided
the *shape of the step sequence* (a ``direct`` flow had PLAN cut out of it), so
its decision and the sequence it implied had to be persisted atomically, be
unwindable on a failed rebuild, and be re-derivable from an old flow's recorded
steps. None of that exists here. feature / bugfix / discovery all run
ANALYZE -> PLAN -> IMPLEMENT unconditionally; what varies is only *what PLAN
emits* (coarse capability groups vs. the legacy fine-grained task listing) and
*how many groups* it emits. Where the granularity left the group count to PLAN,
the execution shape downstream is read off that count, not off a routing flag:
one group is executed as a single whole-task call, two or more enter the
dependency DAG. The one exception is ``plan_granularity: single``, a configured
guarantee rather than a hint to PLAN — it pins the one-call shape however many
groups PLAN emitted. So this module deliberately exposes no
function that accepts or returns a ``StepType`` sequence — "the new model cannot
rewrite the sequence" is a structural fact of the file, not a convention.

The decision is still made exactly once, at flow creation, and persisted: a
resumed flow keeps executing the doctrine it already entered, whatever the
configuration says later.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, MutableMapping, Optional, Tuple


PLAN_DECOMPOSITION_KEY = "plan_decomposition"
PLAN_GRANULARITY_KEY = "plan_granularity"
PLAN_MODE_REASON_KEY = "plan_mode_reason"

#: Context key written by flows created before this model existed. Read-only
#: here: it lets :meth:`PlanModeResolver.view` say what path an old flow
#: actually took instead of reporting a new-model value it never had.
LEGACY_EFFECTIVE_STRATEGY_KEY = "effective_implementation_strategy"


class PlanDecomposition(str, Enum):
    """Which decomposition doctrine PLAN follows."""

    #: Coarse groups sized by "what one autonomous implement call can safely
    #: carry"; a single group is the equivalent of the retired direct path.
    CAPABILITY = "capability"
    #: Retained legacy doctrine: the fine-grained per-task listing.
    GRANULAR = "granular"


class PlanGranularity(str, Enum):
    """Group-count pressure applied under :attr:`PlanDecomposition.CAPABILITY`."""

    AUTO = "auto"
    SINGLE = "single"
    CONSERVATIVE = "conservative"


class PlanModeError(ValueError):
    """Raised when a plan-decomposition or granularity request is invalid."""


#: Best-effort description of an old flow's path, used only for presentation.
#: ``direct`` meant "one autonomous call owns the whole task" — the capability
#: doctrine's single-group shape; ``planned`` meant "fine-grained task groups"
#: — the granular doctrine. ``not_applicable`` carries no doctrine at all.
_LEGACY_STRATEGY_PROJECTION = {
    "direct": (PlanDecomposition.CAPABILITY, PlanGranularity.SINGLE),
    "planned": (PlanDecomposition.GRANULAR, PlanGranularity.AUTO),
}


@dataclass(frozen=True)
class PlanModeView:
    """Read-only plan-mode projection for persisted and legacy flows."""

    decomposition: PlanDecomposition
    granularity: PlanGranularity
    reason: str
    #: Set only for a flow created before this model existed, carrying the
    #: ``direct``/``planned``/``not_applicable`` value it actually recorded.
    legacy_strategy: Optional[str] = None
    #: True when the values above were derived from legacy state rather than
    #: read from persisted new-model keys.
    inferred: bool = False

    def to_dict(self) -> dict:
        """Return the context-shaped primitive values (the write payload)."""
        return {
            PLAN_DECOMPOSITION_KEY: self.decomposition.value,
            PLAN_GRANULARITY_KEY: self.granularity.value,
            PLAN_MODE_REASON_KEY: self.reason,
        }

    def to_projection(self) -> dict:
        """Return the presentation-shaped dict (adds the legacy annotation)."""
        projection = self.to_dict()
        projection["legacy_strategy"] = self.legacy_strategy
        return projection


class PlanModeResolver:
    """Resolve, persist once, and project the PLAN decomposition decision."""

    DEFAULT_DECOMPOSITION = PlanDecomposition.CAPABILITY
    DEFAULT_GRANULARITY = PlanGranularity.AUTO

    @classmethod
    def resolve_requested(
        cls,
        explicit_decomposition: Optional[Any] = None,
        explicit_granularity: Optional[Any] = None,
        configured: Optional[Any] = None,
    ) -> Tuple[PlanDecomposition, PlanGranularity]:
        """Resolve explicit request > project configuration > default."""
        (decomposition, _), (granularity, _) = cls._resolve_with_sources(
            explicit_decomposition,
            explicit_granularity,
            configured,
        )
        return decomposition, granularity

    @classmethod
    def initialize_context(
        cls,
        context: MutableMapping[str, Any],
        *,
        explicit_decomposition: Optional[Any] = None,
        explicit_granularity: Optional[Any] = None,
        configured_workflow: Optional[Any] = None,
    ) -> PlanModeView:
        """Persist the decision for a new flow; idempotent once written.

        WHY write-once: the doctrine and granularity a flow entered stay
        authoritative for its whole life. Re-resolving on resume would let a
        configuration edit mid-flow change how an already-planned set of groups
        is interpreted, which is exactly the drift the persisted decision
        exists to prevent.
        """
        existing = cls._persisted_view(context)
        if existing is not None:
            return existing

        (decomposition, decomposition_source), (
            granularity,
            granularity_source,
        ) = cls._resolve_with_sources(
            explicit_decomposition,
            explicit_granularity,
            configured_workflow,
        )
        view = PlanModeView(
            decomposition=decomposition,
            granularity=granularity,
            reason=cls._build_reason(
                decomposition,
                decomposition_source,
                granularity,
                granularity_source,
            ),
        )
        context.update(view.to_dict())
        return view

    @classmethod
    def view(cls, context: Mapping[str, Any]) -> PlanModeView:
        """Return persisted state, or a non-mutating legacy-compatible view.

        Never writes: an old flow's engine.json is authoritative on resume, and
        describing it must not silently upgrade it to the new model.
        """
        persisted = cls._persisted_view(context)
        if persisted is not None:
            return persisted

        raw_legacy = context.get(LEGACY_EFFECTIVE_STRATEGY_KEY)
        legacy = raw_legacy.strip().lower() if isinstance(raw_legacy, str) else None
        decomposition, granularity = _LEGACY_STRATEGY_PROJECTION.get(
            legacy or "",
            (cls.DEFAULT_DECOMPOSITION, cls.DEFAULT_GRANULARITY),
        )
        return PlanModeView(
            decomposition=decomposition,
            granularity=granularity,
            reason=(
                "Projected from the retired implementation strategy "
                f"{legacy!r} recorded by this flow."
                if legacy
                else "Flow predates the plan-decomposition model; "
                "showing current defaults."
            ),
            legacy_strategy=legacy,
            inferred=True,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @classmethod
    def _resolve_with_sources(
        cls,
        explicit_decomposition: Optional[Any],
        explicit_granularity: Optional[Any],
        configured: Optional[Any],
    ) -> Tuple[
        Tuple[PlanDecomposition, str],
        Tuple[PlanGranularity, str],
    ]:
        if explicit_decomposition is not None:
            decomposition = _coerce(
                PlanDecomposition,
                explicit_decomposition,
                source="plan decomposition request",
            )
            decomposition_source = "explicit"
        else:
            decomposition, decomposition_source = cls._from_configuration(
                configured,
                enum_cls=PlanDecomposition,
                attribute=PLAN_DECOMPOSITION_KEY,
                default=cls.DEFAULT_DECOMPOSITION,
            )

        if explicit_granularity is not None:
            granularity = _coerce(
                PlanGranularity,
                explicit_granularity,
                source="plan granularity request",
            )
            granularity_source = "explicit"
        else:
            granularity, granularity_source = cls._from_configuration(
                configured,
                enum_cls=PlanGranularity,
                attribute=PLAN_GRANULARITY_KEY,
                default=cls.DEFAULT_GRANULARITY,
            )

        return (decomposition, decomposition_source), (granularity, granularity_source)

    @staticmethod
    def _from_configuration(
        configured: Optional[Any],
        *,
        enum_cls,
        attribute: str,
        default,
    ) -> Tuple[Any, str]:
        """Read one axis off a WorkflowConfig-shaped object (or a mapping)."""
        if configured is None:
            return default, "default"
        if isinstance(configured, Mapping):
            raw = configured.get(attribute)
            explicit = configured.get(f"{attribute}_explicit", raw is not None)
        else:
            raw = getattr(configured, attribute, None)
            explicit = getattr(configured, f"{attribute}_explicit", raw is not None)
        if raw is None:
            return default, "default"
        value = _coerce(enum_cls, raw, source=f"workflow.{attribute}")
        # The config dataclass always carries a value, so only the explicitness
        # flag can tell "the project chose this" from "this is the default".
        return value, ("configuration" if explicit else "default")

    @staticmethod
    def _build_reason(
        decomposition: PlanDecomposition,
        decomposition_source: str,
        granularity: PlanGranularity,
        granularity_source: str,
    ) -> str:
        phrases = {
            "explicit": "selected by explicit request",
            "configuration": "selected by project configuration",
            "default": "left at the default",
        }
        return (
            f"Plan decomposition {decomposition.value!r} "
            f"{phrases[decomposition_source]}; group granularity "
            f"{granularity.value!r} {phrases[granularity_source]}."
        )

    @classmethod
    def _persisted_view(
        cls,
        context: Mapping[str, Any],
    ) -> Optional[PlanModeView]:
        if PLAN_DECOMPOSITION_KEY not in context:
            return None
        try:
            decomposition = _coerce(
                PlanDecomposition,
                context.get(PLAN_DECOMPOSITION_KEY),
                source=PLAN_DECOMPOSITION_KEY,
            )
            granularity = _coerce(
                PlanGranularity,
                context.get(PLAN_GRANULARITY_KEY, cls.DEFAULT_GRANULARITY),
                source=PLAN_GRANULARITY_KEY,
            )
        except PlanModeError:
            # Unreadable persisted values fall through to the legacy projection
            # rather than aborting a resume on a presentation-only field.
            return None
        return PlanModeView(
            decomposition=decomposition,
            granularity=granularity,
            reason=str(context.get(PLAN_MODE_REASON_KEY) or ""),
        )


#: Execution shapes that run the whole task through one autonomous call.
HOLISTIC_MODE_SMALL = "small"
HOLISTIC_MODE_SINGLE_GROUP = "single_group"
#: A flow created under the retired strategy axis with ``direct`` that never
#: reached a PLAN step, so it carries no groups at all — the whole-task shape is
#: the only one it can be resumed into. Once such a flow has run PLAN (possible
#: after the upgrade, see :func:`is_legacy_direct_flow`) it reports
#: :data:`HOLISTIC_MODE_SINGLE_GROUP` instead, so its groups are not discarded.
HOLISTIC_MODE_LEGACY_DIRECT = "legacy_direct"


def plan_group_count(inputs: Mapping[str, Any]) -> Optional[int]:
    """Return how many task groups PLAN handed a downstream step.

    ``task_groups`` is the authority; ``plan_group_count`` is only a fallback
    for a step whose groups were externalized away from its inputs. ``None``
    means "PLAN has not been read yet", which callers must not read as "one".
    """
    groups = inputs.get("task_groups")
    if isinstance(groups, list):
        return len(groups)
    count = inputs.get("plan_group_count")
    if isinstance(count, int) and not isinstance(count, bool):
        return count
    return None


def _legacy_direct_projection(
    inputs: Mapping[str, Any],
    context: Mapping[str, Any],
) -> bool:
    """True when the only decision this flow recorded is the retired ``direct``.

    A persisted ``plan_decomposition`` means the new model owns the decision
    and the legacy key is ignored; otherwise the recorded strategy is projected
    through :data:`_LEGACY_STRATEGY_PROJECTION` and this reports whether that
    projection is the capability doctrine's forced-single shape.
    """
    if inputs.get(PLAN_DECOMPOSITION_KEY) or context.get(PLAN_DECOMPOSITION_KEY):
        return False
    raw = context.get(LEGACY_EFFECTIVE_STRATEGY_KEY) or inputs.get(
        LEGACY_EFFECTIVE_STRATEGY_KEY
    )
    if not isinstance(raw, str):
        return False
    projected = _LEGACY_STRATEGY_PROJECTION.get(raw.strip().lower())
    return projected == (PlanDecomposition.CAPABILITY, PlanGranularity.SINGLE)


def effective_mode(
    inputs: Mapping[str, Any],
    context: Mapping[str, Any],
) -> PlanModeView:
    """Return the doctrine/granularity this flow is *executing* under.

    INVARIANT: every step that acts on the decomposition decision — PLAN when
    it picks a prompt, the plan CONFIRM reviewer, and IMPLEMENT when it picks
    an execution shape — must read one and the same value. They used to
    disagree: PLAN went through :meth:`PlanModeResolver.view` (missing keys ->
    the current default, ``capability``) while IMPLEMENT projected the legacy
    strategy alone (missing keys -> not capability). Any flow carrying neither
    a persisted ``plan_decomposition`` nor the legacy ``direct`` — including
    every pre-upgrade ``--type pending`` flow, which recorded the provisional
    ``effective_implementation_strategy: not_applicable`` at creation — was
    therefore planned as capability and implemented as granular: PLAN emitted
    one coarse group with no ``tasks``/``estimated_loc``, and IMPLEMENT fed it
    to the per-task prompt instead of the whole-task autonomous contract.

    Routing every reader through the same projection removes the class of bug
    rather than the one instance: ``view`` still lets an explicitly persisted
    value win, still projects the legacy ``planned`` onto the granular doctrine
    (so a genuinely old plan keeps the per-task LOC scheduling it was written
    under) and the legacy ``direct`` onto capability/single, and only then
    falls back to the current default.

    Step inputs win over flow context because the state machine copies PLAN's
    *recorded* doctrine into them — the authoritative statement of what PLAN
    actually ran, which the flow context may predate. The retired strategy key
    is overlaid from inputs too: a step whose inputs were built before the axis
    retired carries it there rather than in the context.
    """
    lookup = dict(context)
    for key in (
        PLAN_DECOMPOSITION_KEY,
        PLAN_GRANULARITY_KEY,
        LEGACY_EFFECTIVE_STRATEGY_KEY,
    ):
        value = inputs.get(key)
        if value:
            lookup[key] = value
    return PlanModeResolver.view(lookup)


def is_forced_single_group(
    inputs: Mapping[str, Any],
    context: Mapping[str, Any],
) -> bool:
    """True when this flow is pinned to the one-call shape.

    WHY this exists next to the group count: ``single`` is a promise the engine
    owes the user, not a request PLAN is free to decline. Its prompt directive
    can be ignored by the model, and when it is, the plan carries two or more
    groups — so a shape derived from the count alone would hand a forced-single
    flow the exact DAG it asked not to have. Reading the persisted granularity
    keeps the guarantee mechanical, which is what the retired ``direct``
    strategy provided by removing PLAN from the sequence outright.

    WHY a pre-model ``direct`` flow answers True as well: that is the pin it
    recorded under the old vocabulary (the compatibility mapping is exactly
    ``direct`` -> ``plan_granularity: single``), and once such a flow has been
    resumed far enough for PLAN to run, the pin is the only thing left holding
    it to the whole-task shape its user asked for.

    WHY the raw key rather than :func:`effective_mode` here: that projection
    reports a granularity only alongside a persisted doctrine, so a pin
    recorded without one would be silently downgraded to ``auto``. A guarantee
    must not be contingent on a second key being present.

    Only meaningful under the capability doctrine; callers gate on that first.
    """
    value = inputs.get(PLAN_GRANULARITY_KEY) or context.get(PLAN_GRANULARITY_KEY)
    if value is not None:
        return value == PlanGranularity.SINGLE.value
    return _legacy_direct_projection(inputs, context)


def is_capability_decomposition(
    inputs: Mapping[str, Any],
    context: Mapping[str, Any],
) -> bool:
    """True when the flow is running the capability doctrine.

    Shares :func:`effective_mode` with PLAN and the plan CONFIRM reviewer so
    the doctrine a flow plans under is by construction the doctrine it
    implements under; see that function for why a divergent fallback here was
    a defect rather than a nuance.
    """
    return (
        effective_mode(inputs, context).decomposition
        is PlanDecomposition.CAPABILITY
    )


def is_legacy_direct_flow(
    inputs: Mapping[str, Any],
    context: Mapping[str, Any],
) -> bool:
    """True when this flow entered the retired ``direct`` path *and has no plan*.

    WHY the execution shape must still be honoured after the axis retired: a
    ``direct`` flow was created with PLAN cut out of its sequence, so it holds
    no ``task_groups``. Classifying it as grouped work on resume would hand
    IMPLEMENT an empty group list — the group prompt with
    ``## Task Groups\\n[]``, the default invocation intent, no record of the
    previous partial attempt, and a PARTIAL result forwarded to TEST instead of
    re-entered. The decision such a flow persisted is "one autonomous call owns
    the whole task", which is exactly the whole-task shape this module's own
    ``_LEGACY_STRATEGY_PROJECTION`` already reports for it.

    WHY the group count gates it: such a flow *can* acquire a plan after the
    upgrade — ANALYZE rebuilds the sequence from the default table, which now
    always contains PLAN, so a flow interrupted before ANALYZE completed will
    run PLAN under the projected (capability, single) mode and emit proposal,
    design and groups. From that point the flow's own history contradicts this
    branch's prompt ("carries no plan and no task groups"), and the shape must
    be read off the plan instead: :func:`holistic_execution_mode` then lands on
    the single-group branch, which keeps the same one-call contract *and* shows
    the groups as planning context rather than dropping them.
    """
    if not _legacy_direct_projection(inputs, context):
        return False
    return not plan_group_count(inputs)


def holistic_execution_mode(
    *,
    task_type: Optional[str],
    inputs: Mapping[str, Any],
    context: Mapping[str, Any],
) -> Optional[str]:
    """Return the whole-task execution shape, or ``None`` for grouped work.

    WHY the group count rather than a routing flag: where the granularity left
    the group count to PLAN, PLAN's own output is the only authority on the
    execution shape. A separately persisted "holistic" flag would have to be
    kept in sync with ``task_groups`` through plan revisions and adjudication;
    a count derived from the groups themselves cannot drift from them.

    WHY the forced-single check comes first anyway: ``plan_granularity:
    single`` is a configured guarantee rather than a hint to PLAN, so it may
    not be contingent on the model having obeyed a prompt sentence. When it is
    set, the whole task runs as one autonomous call however many groups PLAN
    emitted — the groups then survive only as planning context.

    This is the single decision point for both the IMPLEMENT handler (which
    prompt/executor to run) and the state machine (whether a partial result
    may be auto-continued), so the two can never disagree about the shape —
    including for the legacy ``direct`` flows handled below, which reach here
    with no group count to read at all. A ``direct`` flow that did acquire a
    plan on resume falls through to the ordinary capability branches: its
    recorded strategy projects onto (capability, single), so it keeps the same
    one-call contract while its groups stay visible as planning context.
    """
    if task_type == HOLISTIC_MODE_SMALL:
        return HOLISTIC_MODE_SMALL
    if is_legacy_direct_flow(inputs, context):
        return HOLISTIC_MODE_LEGACY_DIRECT
    if not is_capability_decomposition(inputs, context):
        return None
    if is_forced_single_group(inputs, context):
        return HOLISTIC_MODE_SINGLE_GROUP
    count = plan_group_count(inputs)
    # WHY zero joins one instead of falling through: PLAN now rejects an empty
    # capability plan outright (see steps/plan.py), so a zero here can only
    # come from a plan persisted before that guard existed. Letting it fall
    # through would hand such a flow the grouped contract for an enumeration
    # with nothing in it — the per-group prompt with ``## Task Groups\n[]``,
    # the default invocation intent, and a PARTIAL result forwarded to TEST
    # instead of re-entered. The whole task still has to be delivered by one
    # autonomous call, which is precisely the one-group shape. ``None`` (PLAN
    # not read yet) is deliberately excluded: it is not a count of zero.
    if count is not None and count <= 1:
        return HOLISTIC_MODE_SINGLE_GROUP
    return None


def _coerce(enum_cls, value: Any, *, source: str):
    """Coerce ``value`` into ``enum_cls``, naming the legal set on failure."""
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in enum_cls)
        raise PlanModeError(f"{source}={value!r} must be one of: {allowed}") from exc
