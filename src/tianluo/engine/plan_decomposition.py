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
*how many groups* it emits. The execution shape downstream is then read off the
group count, not off a routing flag. So this module deliberately exposes no
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


def _coerce(enum_cls, value: Any, *, source: str):
    """Coerce ``value`` into ``enum_cls``, naming the legal set on failure."""
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in enum_cls)
        raise PlanModeError(f"{source}={value!r} must be one of: {allowed}") from exc
