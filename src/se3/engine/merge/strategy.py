"""StrategyDecider — Three-strategy decision matrix for conflict resolution.

Implements safe/strict/fast decision logic based on LLM resolution
confidence, spec guardrail flags, and file types.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .conflict_resolver import (
    BatchContext,
    BatchResolveOutcome,
    Confidence,
    ConflictFile,
    LLMResolution,
    MergeStrategy,
)

if TYPE_CHECKING:
    from .conflict_resolver import ConflictResolver

logger = logging.getLogger(__name__)


class DecisionAction(str, Enum):
    """Final action decided by the strategy decider."""

    ACCEPT = "accept"
    HUMAN_CALL = "human_call"
    REJECT = "reject"


@dataclass
class StrategyDecision:
    """Result of strategy evaluation."""

    action: DecisionAction
    reason: str = ""
    # Populated by the new ``resolve_and_decide`` path so the orchestrator
    # can build a human-call file (safe / strict) or a precise failure
    # report (fast) without re-running the resolver.
    outcome: Optional[BatchResolveOutcome] = None
    unresolved_files: list[Path] = field(default_factory=list)


class StrategyDecider:
    """Decide whether to accept, reject, or escalate to human review.

    Three strategies:
    - safe:    high confidence + no spec_guardrail_concern → ACCEPT,
               otherwise HUMAN_CALL (the legacy ``default`` tier).
    - strict:  short-circuited by orchestrator (_handle_conflict skips LLM
               and goes directly to human call). Retained as fallback.
    - fast:    ACCEPT or REJECT only — never HUMAN_CALL.
               Regular files → aggressive accept (low confidence ok).
               Spec files → REJECT on requires_human_review or low confidence.
               spec_guardrail_concern is deferred to post-merge guardrails.
    """

    def decide(
        self,
        resolution: LLMResolution,
        has_spec_files: bool,
        strategy: MergeStrategy = MergeStrategy.SAFE,
    ) -> StrategyDecision:
        """Evaluate the LLM resolution and return a decision.

        Args:
            resolution: The LLM's structured resolution output.
            has_spec_files: Whether the merge involves spec files.
            strategy: The conflict resolution strategy.

        Returns:
            StrategyDecision with action and reason.
        """
        if strategy == MergeStrategy.SAFE:
            return self._decide_safe(resolution, has_spec_files)
        elif strategy == MergeStrategy.STRICT:
            return self._decide_strict(resolution, has_spec_files)
        elif strategy == MergeStrategy.FAST:
            return self._decide_fast(resolution)
        else:
            logger.warning("Unknown strategy %s, falling back to safe", strategy)
            return self._decide_safe(resolution, has_spec_files)

    def _decide_safe(
        self,
        resolution: LLMResolution,
        has_spec_files: bool,
    ) -> StrategyDecision:
        """Safe strategy: high overall confidence + no spec concerns → ACCEPT.

        Unlike strict, safe does NOT require every individual hunk to be
        HIGH — only file-level and global overall confidence.

        Note:
            ``has_spec_files`` is kept in the signature for API consistency
            with ``decide()`` but is intentionally unused; safe strategy
            relies on per-file ``is_spec`` and confidence flags rather than
            a global file-type predicate.
        """
        # Check global flags first
        if resolution.flags.get("spec_guardrail_concern", False):
            return StrategyDecision(
                action=DecisionAction.HUMAN_CALL,
                reason="spec_guardrail_concern flag set (safe strategy)",
            )

        if resolution.flags.get("requires_human_review", False):
            return StrategyDecision(
                action=DecisionAction.HUMAN_CALL,
                reason="requires_human_review flag set (safe strategy)",
            )

        # Check per-file flags and file-level overall confidence
        for f in resolution.files:
            if f.flags.get("spec_guardrail_concern", False):
                return StrategyDecision(
                    action=DecisionAction.HUMAN_CALL,
                    reason=f"spec_guardrail_concern on file {f.path} (safe strategy)",
                )
            if f.flags.get("requires_human_review", False):
                return StrategyDecision(
                    action=DecisionAction.HUMAN_CALL,
                    reason=f"requires_human_review on file {f.path} (safe strategy)",
                )
            # Safe requires file-level overall confidence to be HIGH
            if f.overall_confidence != Confidence.HIGH:
                return StrategyDecision(
                    action=DecisionAction.HUMAN_CALL,
                    reason=f"overall confidence on {f.path} is {f.overall_confidence.value}, not high (safe strategy)",
                )
            # Note: safe does NOT gate on per-hunk confidence — that is
            # the distinguishing factor from strict.

        # Require high global overall confidence
        if resolution.overall_confidence != Confidence.HIGH:
            return StrategyDecision(
                action=DecisionAction.HUMAN_CALL,
                reason=f"overall confidence is {resolution.overall_confidence.value}, not high (safe strategy)",
            )

        # All checks passed
        return StrategyDecision(
            action=DecisionAction.ACCEPT,
            reason="High confidence, no guardrail concerns (safe strategy)",
        )

    def _decide_strict(
        self,
        resolution: LLMResolution,
        has_spec_files: bool,
    ) -> StrategyDecision:
        """Strict strategy: placeholder — orchestrator short-circuits this path.

        In practice, the orchestrator handles strict strategy in
        ``_handle_conflict`` by skipping LLM resolution and writing a human
        call directly. This method is retained as a fallback for unexpected
        code paths (e.g., if the orchestrator's short-circuit is bypassed).

        Note:
            ``has_spec_files`` is kept in the signature for API consistency
            with ``decide()`` but is intentionally unused; strict strategy
            gates on per-hunk confidence, not on whether spec files are
            present in the merge.
        """
        # Check global flags
        if resolution.flags.get("spec_guardrail_concern", False):
            return StrategyDecision(
                action=DecisionAction.HUMAN_CALL,
                reason="spec_guardrail_concern flag set (strict strategy)",
            )

        if resolution.flags.get("requires_human_review", False):
            return StrategyDecision(
                action=DecisionAction.HUMAN_CALL,
                reason="requires_human_review flag set (strict strategy)",
            )

        # Check every hunk in every file must be high confidence
        for f in resolution.files:
            if f.flags.get("spec_guardrail_concern", False):
                return StrategyDecision(
                    action=DecisionAction.HUMAN_CALL,
                    reason=f"spec_guardrail_concern on file {f.path} (strict strategy)",
                )
            if f.flags.get("requires_human_review", False):
                return StrategyDecision(
                    action=DecisionAction.HUMAN_CALL,
                    reason=f"requires_human_review on file {f.path} (strict strategy)",
                )
            if f.overall_confidence != Confidence.HIGH:
                return StrategyDecision(
                    action=DecisionAction.HUMAN_CALL,
                    reason=f"overall confidence on {f.path} is {f.overall_confidence.value}, not high (strict strategy)",
                )
            for hunk in f.hunks:
                if hunk.confidence != Confidence.HIGH:
                    return StrategyDecision(
                        action=DecisionAction.HUMAN_CALL,
                        reason=f"hunk {hunk.start_line}-{hunk.end_line} in {f.path} confidence is {hunk.confidence.value}, not high (strict strategy)",
                    )

        # Require high overall confidence too
        if resolution.overall_confidence != Confidence.HIGH:
            return StrategyDecision(
                action=DecisionAction.HUMAN_CALL,
                reason=f"overall confidence is {resolution.overall_confidence.value}, not high (strict strategy)",
            )

        return StrategyDecision(
            action=DecisionAction.ACCEPT,
            reason="All hunks high confidence, no guardrail concerns (strict strategy)",
        )

    def _decide_fast(
        self,
        resolution: LLMResolution,
    ) -> StrategyDecision:
        """Fast strategy: aggressive accept for regular files; spec quality gates → REJECT.

        Fast mode NEVER returns HUMAN_CALL — only ACCEPT or REJECT.
        - Regular (non-spec) files: always accepted regardless of confidence.
        - Spec files: REJECTed if the LLM explicitly asks for human review or
          reports low confidence.  ``spec_guardrail_concern`` is ignored here
          because fast mode delegates spec violations to post-merge guardrails
          (and, if needed, ``GuardrailRepairer``).
        - Any REJECT is translated by the orchestrator into a clean abort with
          no human call file written.
        """
        # requires_human_review at global level → REJECT (fast never calls human)
        if resolution.flags.get("requires_human_review", False):
            return StrategyDecision(
                action=DecisionAction.REJECT,
                reason="requires_human_review flag set (fast strategy aborts, no human call)",
            )

        # Global spec_guardrail_concern → log and defer (post-merge guardrails handle it)
        if resolution.flags.get("spec_guardrail_concern", False):
            logger.info(
                "Fast strategy: deferring global spec_guardrail_concern "
                "to post-merge guardrails"
            )

        for f in resolution.files:
            if f.is_spec:
                if f.flags.get("spec_guardrail_concern", False):
                    logger.info(
                        "Fast strategy: deferring spec_guardrail_concern on "
                        "spec file %s to post-merge guardrails",
                        f.path,
                    )
                if f.flags.get("requires_human_review", False):
                    return StrategyDecision(
                        action=DecisionAction.REJECT,
                        reason=f"requires_human_review on spec file {f.path} (fast strategy aborts, no human call)",
                    )
                # Spec files still need high confidence even in fast mode
                if f.overall_confidence != Confidence.HIGH:
                    return StrategyDecision(
                        action=DecisionAction.REJECT,
                        reason=f"overall confidence on spec file {f.path} is {f.overall_confidence.value}, not high (fast strategy aborts, no human call)",
                    )
        # Collect any pathological flags on non-spec files for visibility
        dropped_flags: list[str] = []
        for f in resolution.files:
            if not f.is_spec:
                if f.flags.get("spec_guardrail_concern", False):
                    dropped_flags.append(f"spec_guardrail_concern on {f.path}")
                if f.flags.get("requires_human_review", False):
                    dropped_flags.append(f"requires_human_review on {f.path}")
                    logger.warning(
                        "Fast strategy accepted non-spec file %s despite per-file "
                        "requires_human_review flag from LLM",
                        f.path,
                    )

        reason = "Fast strategy: accepted (spec_guardrail_concern deferred to post-merge guardrails)"
        if dropped_flags:
            reason += "; WARNING: " + ", ".join(dropped_flags)

        return StrategyDecision(
            action=DecisionAction.ACCEPT,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # New batch / LLM-as-editor decision path (G3).
    # ------------------------------------------------------------------

    def resolve_and_decide(
        self,
        resolver: "ConflictResolver",
        conflict_files: list[ConflictFile],
        context: BatchContext,
        *,
        max_iterations: int,
    ) -> StrategyDecision:
        """Run :meth:`ConflictResolver.resolve_batch` and translate the
        outcome into a :class:`StrategyDecision` per the active merge
        strategy.

        Branching:

        * ``MergeStrategy.STRICT`` — short-circuits inside ``resolve_batch``
          (no LLM call); every conflict file routes straight to a human
          MCP call.
        * ``MergeStrategy.FAST`` — success → ACCEPT; hitting
          ``max_iterations`` with files still unresolved → REJECT, never
          HUMAN_CALL.
        * ``MergeStrategy.SAFE`` — success → ACCEPT; hitting
          ``max_iterations`` → HUMAN_CALL (the human MCP fallback).

        ``take-theirs`` is no longer a possible outcome on any branch.
        """
        strategy = context.strategy

        if strategy == MergeStrategy.STRICT:
            return self._decide_strict_batch(
                resolver, conflict_files, context, max_iterations,
            )
        if strategy == MergeStrategy.FAST:
            return self._decide_fast_batch(
                resolver, conflict_files, context, max_iterations,
            )
        if strategy == MergeStrategy.SAFE:
            return self._decide_safe_batch(
                resolver, conflict_files, context, max_iterations,
            )
        logger.warning(
            "Unknown strategy %s in resolve_and_decide; falling back to safe",
            strategy,
        )
        return self._decide_safe_batch(
            resolver, conflict_files, context, max_iterations,
        )

    def _decide_fast_batch(
        self,
        resolver: "ConflictResolver",
        conflict_files: list[ConflictFile],
        context: BatchContext,
        max_iterations: int,
    ) -> StrategyDecision:
        """Fast strategy: ACCEPT on success, REJECT on exhaustion.

        Never invokes a human MCP call.  When the LLM cannot clear all
        conflict markers within ``max_iterations``, the merge is
        rejected and the orchestrator aborts with a failure report.
        """
        outcome = resolver.resolve_batch(
            conflict_files, context, max_iterations=max_iterations,
        )
        if outcome.success:
            return StrategyDecision(
                action=DecisionAction.ACCEPT,
                reason=(
                    f"Fast strategy: LLM cleared all conflict markers in "
                    f"{outcome.iterations_used} iteration(s)"
                ),
                outcome=outcome,
            )
        return StrategyDecision(
            action=DecisionAction.REJECT,
            reason=(
                f"Fast strategy: LLM could not clear conflict markers in "
                f"{outcome.iterations_used}/{max_iterations} iteration(s); "
                f"{len(outcome.unresolved)} file(s) remain unresolved"
            ),
            outcome=outcome,
            unresolved_files=list(outcome.unresolved),
        )

    def _decide_safe_batch(
        self,
        resolver: "ConflictResolver",
        conflict_files: list[ConflictFile],
        context: BatchContext,
        max_iterations: int,
    ) -> StrategyDecision:
        """Safe strategy: ACCEPT on success, HUMAN_CALL on exhaustion.

        Mirrors the legacy ``default`` semantics but without ever
        delegating to take-theirs — humans, not the resolver, decide
        what to do when the LLM cannot converge.
        """
        outcome = resolver.resolve_batch(
            conflict_files, context, max_iterations=max_iterations,
        )
        if outcome.success:
            return StrategyDecision(
                action=DecisionAction.ACCEPT,
                reason=(
                    f"Safe strategy: LLM cleared all conflict markers in "
                    f"{outcome.iterations_used} iteration(s)"
                ),
                outcome=outcome,
            )
        # Mark the outcome's escalation_reason so callers can route the
        # human-call file write through the safe-specific code path.
        outcome.escalation_reason = "safe_to_human"
        return StrategyDecision(
            action=DecisionAction.HUMAN_CALL,
            reason=(
                f"Safe strategy: LLM could not clear conflict markers in "
                f"{outcome.iterations_used}/{max_iterations} iteration(s); "
                f"escalating {len(outcome.unresolved)} file(s) to human review"
            ),
            outcome=outcome,
            unresolved_files=list(outcome.unresolved),
        )

    def _decide_strict_batch(
        self,
        resolver: "ConflictResolver",
        conflict_files: list[ConflictFile],
        context: BatchContext,
        max_iterations: int,
    ) -> StrategyDecision:
        """Strict strategy: every conflict file routes directly to a human.

        :meth:`ConflictResolver.resolve_batch` short-circuits for
        ``MergeStrategy.STRICT`` and returns immediately with
        ``escalation_reason='strict_to_human'`` — the LLM is never
        invoked.
        """
        outcome = resolver.resolve_batch(
            conflict_files, context, max_iterations=max_iterations,
        )
        return StrategyDecision(
            action=DecisionAction.HUMAN_CALL,
            reason=(
                f"Strict strategy: routing {len(conflict_files)} conflict "
                "file(s) directly to human review (LLM not invoked)"
            ),
            outcome=outcome,
            unresolved_files=list(outcome.unresolved),
        )

