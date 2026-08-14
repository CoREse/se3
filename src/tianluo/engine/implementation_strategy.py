"""Implementation-strategy data contract and compatibility resolution.

Implementation strategy is deliberately narrower than task type: it only
selects the execution shape of a workflow segment that originally contains
both PLAN and IMPLEMENT.  It exposes the one shared sequence transform used by
new-flow creation and ANALYZE rebuilding so those paths cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping, MutableMapping, Optional, Sequence

from .models import (
    EffectiveImplementationStrategy,
    RequestedImplementationStrategy,
    StepType,
    get_default_step_sequence,
)


REQUESTED_IMPLEMENTATION_STRATEGY_KEY = "requested_implementation_strategy"
EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY = "effective_implementation_strategy"
IMPLEMENTATION_STRATEGY_REASON_KEY = "strategy_reason"
# Set only once ANALYZE has finalized the strategy against its resolved task
# type. ``initialize_context`` writes a PROVISIONAL effective value (driven by
# the creation-time task type) without this flag; ``finalize_for_analyze``
# re-derives from the ANALYZE-resolved type and stamps the flag, making the
# one-time decision immutable afterward — a reclassification can therefore
# correct a provisional value, while nothing later can rewrite the finalized
# path a running or resumed flow is already executing.
IMPLEMENTATION_STRATEGY_FINALIZED_KEY = "implementation_strategy_finalized"
AUTO_STRATEGY_FIELD = "implementation_strategy"
AUTO_STRATEGY_REASON_FIELD = "strategy_reason"
AUTO_FALLBACK_REASON = (
    "ANALYZE returned no valid direct|planned recommendation and reason; "
    "defaulted to planned."
)
AUTO_UNPARSEABLE_FALLBACK_REASON = (
    "ANALYZE returned a direct|planned recommendation that could not be "
    "honored (unparseable value or missing reason); defaulted to planned."
)
#: Used when the ANALYZE call carried no strategy question — a flow whose
#: creation-time task type gained its PLAN -> IMPLEMENT surface only at
#: classification time — and the response volunteered no recommendation.
#: Recording AUTO_FALLBACK_REASON there would assert a request that never
#: happened.
AUTO_NOT_REQUESTED_FALLBACK_REASON = (
    "ANALYZE was not asked for a direct|planned recommendation and returned "
    "none; defaulted to planned."
)


class ImplementationStrategyError(ValueError):
    """Raised when an implementation-strategy request is invalid."""


@dataclass(frozen=True)
class ImplementationStrategyView:
    """Read-only strategy projection for persisted and legacy flows."""

    # ``requested`` is None only for the unknown legacy projection (no
    # persisted request and nothing on disk to infer from).
    requested: Optional[RequestedImplementationStrategy]
    effective: Optional[EffectiveImplementationStrategy]
    reason: str
    inferred: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return context-shaped primitive values for presentation layers."""
        return {
            REQUESTED_IMPLEMENTATION_STRATEGY_KEY: (
                self.requested.value if self.requested is not None else None
            ),
            EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY: (
                self.effective.value if self.effective is not None else None
            ),
            IMPLEMENTATION_STRATEGY_REASON_KEY: self.reason,
        }


class ImplementationStrategyResolver:
    """Resolve new-flow requests and expose legacy state without mutation."""

    DEFAULT = RequestedImplementationStrategy.PLANNED

    @classmethod
    def resolve_requested(
        cls,
        explicit_request: Optional[Any] = None,
        configured_strategy: Optional[Any] = None,
    ) -> RequestedImplementationStrategy:
        """Resolve explicit request > project configuration > default."""
        if explicit_request is not None:
            return cls._coerce_requested(
                explicit_request, source="implementation strategy request"
            )
        if configured_strategy is not None:
            configured_value = getattr(
                configured_strategy,
                "implementation_strategy",
                configured_strategy,
            )
            return cls._coerce_requested(
                configured_value,
                source="workflow.implementation_strategy",
            )
        return cls.DEFAULT

    @staticmethod
    def _coerce_requested(
        value: Any,
        *,
        source: str,
    ) -> RequestedImplementationStrategy:
        if isinstance(value, RequestedImplementationStrategy):
            return value
        try:
            return RequestedImplementationStrategy(value)
        except (TypeError, ValueError) as exc:
            allowed = ", ".join(item.value for item in RequestedImplementationStrategy)
            raise ImplementationStrategyError(
                f"{source}={value!r} must be one of: {allowed}"
            ) from exc

    @classmethod
    def has_choice_surface(cls, task_type: Optional[str]) -> bool:
        """Return whether the task type's original sequence has PLAN and IMPLEMENT.

        WHY: the choice surface is defined by the task description's criterion
        — the implementation phase that originally contains PLAN -> IMPLEMENT —
        so it is derived from the actual default sequence, not a hardcoded
        type list. ``get_default_step_sequence`` falls back to the feature
        sequence for types without their own table entry (``investigate`` and
        ``refactor`` flows created via preset or the engine API), and those
        flows really do run PLAN -> IMPLEMENT — an explicit direct request
        must remove that PLAN pair instead of being silently ignored as
        "not applicable". ``pending`` keeps deferring to the ANALYZE-resolved
        type, never to the fallback sequence.
        """
        if not task_type or task_type == "pending":
            return False
        sequence = get_default_step_sequence(task_type)
        return StepType.PLAN in sequence and StepType.IMPLEMENT in sequence

    is_applicable = has_choice_surface

    @classmethod
    def should_request_auto_recommendation(
        cls,
        context: Mapping[str, Any],
        *,
        task_type: Optional[str],
    ) -> bool:
        """Return whether this ANALYZE call should ask for a strategy choice.

        WHY the creation-time ``task_type`` cannot gate this: ANALYZE is what
        RESOLVES the type, and it may reclassify any flow (a preset-created
        'small' becomes 'feature'). The surface that decides applicability is
        the ANALYZE-RESOLVED one, which does not exist yet when this prompt is
        built — so an AUTO request that is not yet finalized always asks, and
        the prompt itself makes the field conditional on the model's own
        classification having a PLAN -> IMPLEMENT surface. Gating on the
        provisional ``effective`` (``initialize_context`` writes
        ``not_applicable`` for a no-surface creation-time type) would silence
        the question for exactly the flows that get reclassified INTO the
        choice surface, and then record a reason asserting a recommendation
        was requested and missing.
        """
        persisted = cls._persisted_view(context)
        if (
            persisted is None
            or persisted.requested is not RequestedImplementationStrategy.AUTO
        ):
            return False
        # Finalization — not the provisional effective value — is the one-time
        # decision this gate must respect.
        return context.get(IMPLEMENTATION_STRATEGY_FINALIZED_KEY) is not True

    @classmethod
    def finalize_for_analyze(
        cls,
        context: MutableMapping[str, Any],
        *,
        task_type: str,
        analyze_output: Mapping[str, Any],
        recommendation_requested: bool,
        selected_steps: Sequence[Any] = (),
    ) -> ImplementationStrategyView:
        """Finalize a new flow's strategy once ANALYZE resolves its task type.

        The ANALYZE-resolved task type is the authority: a provisional
        effective value written by ``initialize_context`` (derived from the
        creation-time type) is re-derived here — a reclassification to a
        no-surface type finalizes ``not_applicable``, and one to a
        choice-surface type honors an explicit non-auto request. The decision
        is stamped once; later calls (or resumed flows whose ANALYZE already
        ran) keep the finalized result.
        """
        persisted = cls._persisted_view(context)
        if persisted is None:
            # Legacy flows keep their recorded path authoritative and are never
            # upgraded merely because they crossed this newer ANALYZE handler.
            return cls.view(
                context,
                task_type=task_type,
                selected_steps=selected_steps,
            )
        if context.get(IMPLEMENTATION_STRATEGY_FINALIZED_KEY) is True:
            # Already finalized by a previous ANALYZE: the one-time decision
            # is immutable — implementation 开始后不再重新决策，resume 沿用已
            # 持久化结果，配置变化不得改变正在执行的路径.
            return persisted

        if not cls.has_choice_surface(task_type):
            return cls._write_finalized(
                context,
                ImplementationStrategyView(
                    requested=persisted.requested,
                    effective=EffectiveImplementationStrategy.NOT_APPLICABLE,
                    reason=(
                        f"Task type {task_type!r} has no PLAN -> IMPLEMENT "
                        "strategy surface."
                    ),
                ),
            )

        if persisted.requested is not RequestedImplementationStrategy.AUTO:
            return cls._write_finalized(
                context,
                ImplementationStrategyView(
                    requested=persisted.requested,
                    effective=EffectiveImplementationStrategy(
                        persisted.requested.value
                    ),
                    reason=(
                        f"Finalized requested {persisted.requested.value!r} "
                        "strategy after ANALYZE resolved an applicable "
                        "task type."
                    ),
                ),
            )

        recommendation = analyze_output.get(AUTO_STRATEGY_FIELD)
        reason = analyze_output.get(AUTO_STRATEGY_REASON_FIELD)
        if reason is None:
            # Accept the initial development spelling so prerelease call
            # history remains resumable while the persisted field uses the
            # public strategy_reason contract.
            reason = analyze_output.get("implementation_strategy_reason")
        # Normalize before matching so an incidental "direct " or "Direct"
        # still honors the recommendation instead of silently re-planning.
        normalized_recommendation = (
            recommendation.strip().lower()
            if isinstance(recommendation, str) and recommendation.strip()
            else None
        )
        valid_reason = isinstance(reason, str) and bool(reason.strip())
        # A valid recommendation is honored whether or not the prompt asked for
        # it: the model classified the task type in the same response, so a
        # spontaneous direct|planned choice with a reason is exactly the
        # decision this step exists to record — discarding it would default to
        # planned while a usable answer sits in the output.
        if normalized_recommendation in ("direct", "planned") and valid_reason:
            return cls._write_finalized(
                context,
                ImplementationStrategyView(
                    requested=persisted.requested,
                    effective=EffectiveImplementationStrategy(
                        normalized_recommendation
                    ),
                    reason=reason.strip(),
                ),
            )

        if normalized_recommendation is not None:
            return cls._write_finalized(
                context,
                ImplementationStrategyView(
                    requested=persisted.requested,
                    effective=EffectiveImplementationStrategy.PLANNED,
                    reason=AUTO_UNPARSEABLE_FALLBACK_REASON,
                ),
            )
        return cls._write_finalized(
            context,
            ImplementationStrategyView(
                requested=persisted.requested,
                effective=EffectiveImplementationStrategy.PLANNED,
                # The recorded reason must describe what actually happened:
                # asserting a requested-but-missing recommendation for a call
                # that never carried the question is false, and it is shown
                # verbatim in the CLI summary, the WebUI and history JSON.
                reason=(
                    AUTO_FALLBACK_REASON
                    if recommendation_requested
                    else AUTO_NOT_REQUESTED_FALLBACK_REASON
                ),
            ),
        )

    #: Every context key the resolver owns; snapshot/restore operate on all of
    #: them together so a partially-applied decision can be unwound as a unit.
    CONTEXT_KEYS = (
        REQUESTED_IMPLEMENTATION_STRATEGY_KEY,
        EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY,
        IMPLEMENTATION_STRATEGY_REASON_KEY,
        IMPLEMENTATION_STRATEGY_FINALIZED_KEY,
    )

    @classmethod
    def snapshot_context(cls, context: Mapping[str, Any]) -> dict[str, Any]:
        """Capture the strategy keys so a failed transform can unwind them."""
        return {key: context[key] for key in cls.CONTEXT_KEYS if key in context}

    @classmethod
    def restore_context(
        cls,
        context: MutableMapping[str, Any],
        snapshot: Mapping[str, Any],
    ) -> None:
        """Restore a snapshot, removing keys the snapshot did not contain."""
        for key in cls.CONTEXT_KEYS:
            if key in snapshot:
                context[key] = snapshot[key]
            else:
                context.pop(key, None)

    @staticmethod
    def _write_finalized(
        context: MutableMapping[str, Any],
        view: ImplementationStrategyView,
    ) -> ImplementationStrategyView:
        """Persist the one-time decision and stamp it as finalized."""
        context.update(view.to_dict())
        context[IMPLEMENTATION_STRATEGY_FINALIZED_KEY] = True
        return view

    @staticmethod
    def apply_to_steps(
        steps: Sequence[StepType],
        effective: Optional[Any],
    ) -> List[StepType]:
        """Apply a finalized strategy without disturbing other quality gates."""
        raw_effective = getattr(effective, "value", effective)
        if raw_effective != EffectiveImplementationStrategy.DIRECT.value:
            return list(steps)

        result: List[StepType] = []
        removed_plan_awaiting_confirm = False
        for step in steps:
            if step is StepType.PLAN:
                removed_plan_awaiting_confirm = True
                continue
            if removed_plan_awaiting_confirm and step is StepType.CONFIRM:
                removed_plan_awaiting_confirm = False
                continue
            removed_plan_awaiting_confirm = False
            result.append(step)
        return result

    @classmethod
    def infer_legacy_effective(
        cls,
        task_type: Optional[str],
        selected_steps: Sequence[Any],
    ) -> Optional[EffectiveImplementationStrategy]:
        """Infer an old flow's path from its persisted task type and steps.

        WHY: old state must remain byte-for-byte authoritative on resume.  The
        inference describes the path already recorded by ``selected_steps``;
        it never upgrades or rewrites that state.

        ``None`` means "nothing on disk to infer from": with no recorded steps
        and a type that could carry either path, the flow's strategy is
        unknown, not ``not_applicable``. That is the same answer
        :func:`tianluo.strategy_view.strategy_view` gives, and the two
        projections must agree — otherwise one flow reads 'not_applicable' in
        engine surfaces and 'unknown' in the daemon/WebUI/history view.
        """
        normalized = {
            step.value if isinstance(step, StepType) else str(step)
            for step in selected_steps
        }
        if task_type in ("small", "review", "survey"):
            return EffectiveImplementationStrategy.NOT_APPLICABLE
        if not normalized:
            return None
        if (
            StepType.PLAN.value in normalized
            and StepType.IMPLEMENT.value in normalized
        ):
            return EffectiveImplementationStrategy.PLANNED
        if cls.has_choice_surface(task_type) and StepType.IMPLEMENT.value in normalized:
            return EffectiveImplementationStrategy.DIRECT
        return EffectiveImplementationStrategy.NOT_APPLICABLE

    @classmethod
    def initialize_context(
        cls,
        context: MutableMapping[str, Any],
        *,
        task_type: Optional[str],
        selected_steps: Sequence[Any],
        explicit_request: Optional[Any] = None,
        configured_strategy: Optional[Any] = None,
    ) -> ImplementationStrategyView:
        """Persist the strategy request for a new flow without changing steps."""
        existing = cls._persisted_view(context)
        if existing is not None:
            return existing

        requested = cls.resolve_requested(explicit_request, configured_strategy)
        if explicit_request is not None:
            request_reason = "Selected by explicit request."
        elif configured_strategy is not None:
            request_reason = "Selected by project configuration."
        else:
            request_reason = "Selected by the default implementation strategy."

        if not task_type or task_type == "pending":
            effective: Optional[EffectiveImplementationStrategy] = None
            reason = "Pending task-type resolution in ANALYZE."
        elif not cls.has_choice_surface(task_type):
            effective = EffectiveImplementationStrategy.NOT_APPLICABLE
            reason = (
                f"Task type {task_type!r} has no PLAN -> IMPLEMENT strategy surface."
            )
        elif requested is RequestedImplementationStrategy.AUTO:
            effective = None
            reason = "Pending the one-time ANALYZE strategy recommendation."
        else:
            effective = EffectiveImplementationStrategy(requested.value)
            reason = request_reason

        view = ImplementationStrategyView(
            requested=requested,
            effective=effective,
            reason=reason,
        )
        context.update(view.to_dict())
        return view

    @classmethod
    def persist_effective(
        cls,
        context: MutableMapping[str, Any],
        effective: Any,
        reason: str,
    ) -> ImplementationStrategyView:
        """Persist a decision once; a resumed flow keeps the existing result."""
        existing = cls._persisted_view(context)
        if existing is not None and existing.effective is not None:
            return existing

        try:
            effective_value = (
                effective
                if isinstance(effective, EffectiveImplementationStrategy)
                else EffectiveImplementationStrategy(effective)
            )
        except (TypeError, ValueError) as exc:
            allowed = ", ".join(
                item.value for item in EffectiveImplementationStrategy
            )
            raise ImplementationStrategyError(
                f"effective implementation strategy={effective!r} "
                f"must be one of: {allowed}"
            ) from exc

        requested = cls._requested_from_context(context)
        view = ImplementationStrategyView(
            requested=requested,
            effective=effective_value,
            reason=str(reason or ""),
        )
        context.update(view.to_dict())
        return view

    @classmethod
    def view(
        cls,
        context: Mapping[str, Any],
        *,
        task_type: Optional[str],
        selected_steps: Sequence[Any],
    ) -> ImplementationStrategyView:
        """Return persisted state or a non-mutating legacy compatibility view."""
        persisted = cls._persisted_view(context)
        if persisted is not None:
            return persisted

        inferred = cls.infer_legacy_effective(task_type, selected_steps)
        if inferred is None:
            # Nothing recoverable: report unknown rather than a fabricated
            # path (mirrors the control-plane projection exactly).
            return ImplementationStrategyView(
                requested=None,
                effective=None,
                reason="",
                inferred=False,
            )
        requested = (
            RequestedImplementationStrategy.PLANNED
            if inferred is not EffectiveImplementationStrategy.DIRECT
            else RequestedImplementationStrategy.DIRECT
        )
        return ImplementationStrategyView(
            requested=requested,
            effective=inferred,
            reason="Inferred from persisted legacy task type and selected_steps.",
            inferred=True,
        )

    @classmethod
    def _persisted_view(
        cls,
        context: Mapping[str, Any],
    ) -> Optional[ImplementationStrategyView]:
        if REQUESTED_IMPLEMENTATION_STRATEGY_KEY not in context:
            return None

        try:
            requested = cls._coerce_requested(
                context.get(REQUESTED_IMPLEMENTATION_STRATEGY_KEY),
                source=REQUESTED_IMPLEMENTATION_STRATEGY_KEY,
            )
        except ImplementationStrategyError:
            return None

        raw_effective = context.get(EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY)
        if raw_effective is None:
            effective = None
        else:
            try:
                effective = EffectiveImplementationStrategy(raw_effective)
            except (TypeError, ValueError):
                return None

        return ImplementationStrategyView(
            requested=requested,
            effective=effective,
            reason=str(context.get(IMPLEMENTATION_STRATEGY_REASON_KEY) or ""),
        )

    @classmethod
    def _requested_from_context(
        cls,
        context: Mapping[str, Any],
    ) -> RequestedImplementationStrategy:
        raw = context.get(REQUESTED_IMPLEMENTATION_STRATEGY_KEY, cls.DEFAULT.value)
        return cls._coerce_requested(raw, source=REQUESTED_IMPLEMENTATION_STRATEGY_KEY)
