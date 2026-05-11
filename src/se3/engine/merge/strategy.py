"""StrategyDecider — Three-strategy decision matrix for conflict resolution.

Implements default/strict/fast decision logic based on LLM resolution
confidence, spec guardrail flags, and file types.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from .conflict_resolver import Confidence, LLMResolution, MergeStrategy

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


class StrategyDecider:
    """Decide whether to accept, reject, or escalate to human review.

    Three strategies:
    - default: high confidence + no spec_guardrail_concern → ACCEPT,
               otherwise HUMAN_CALL
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
        strategy: MergeStrategy = MergeStrategy.DEFAULT,
    ) -> StrategyDecision:
        """Evaluate the LLM resolution and return a decision.

        Args:
            resolution: The LLM's structured resolution output.
            has_spec_files: Whether the merge involves spec files.
            strategy: The conflict resolution strategy.

        Returns:
            StrategyDecision with action and reason.
        """
        if strategy == MergeStrategy.DEFAULT:
            return self._decide_default(resolution, has_spec_files)
        elif strategy == MergeStrategy.STRICT:
            return self._decide_strict(resolution, has_spec_files)
        elif strategy == MergeStrategy.FAST:
            return self._decide_fast(resolution)
        elif strategy == MergeStrategy.ROBUST:
            return self._decide_robust(resolution, has_spec_files)
        else:
            logger.warning("Unknown strategy %s, falling back to default", strategy)
            return self._decide_default(resolution, has_spec_files)

    def _decide_default(
        self,
        resolution: LLMResolution,
        has_spec_files: bool,
    ) -> StrategyDecision:
        """Default strategy: high overall confidence + no spec concerns → ACCEPT.

        Unlike strict, default does NOT require every individual hunk to be
        HIGH — only file-level and global overall confidence.

        Note:
            ``has_spec_files`` is kept in the signature for API consistency
            with ``decide()`` but is intentionally unused; default strategy
            relies on per-file ``is_spec`` and confidence flags rather than
            a global file-type predicate.
        """
        # Check global flags first
        if resolution.flags.get("spec_guardrail_concern", False):
            return StrategyDecision(
                action=DecisionAction.HUMAN_CALL,
                reason="spec_guardrail_concern flag set (default strategy)",
            )

        if resolution.flags.get("requires_human_review", False):
            return StrategyDecision(
                action=DecisionAction.HUMAN_CALL,
                reason="requires_human_review flag set (default strategy)",
            )

        # Check per-file flags and file-level overall confidence
        for f in resolution.files:
            if f.flags.get("spec_guardrail_concern", False):
                return StrategyDecision(
                    action=DecisionAction.HUMAN_CALL,
                    reason=f"spec_guardrail_concern on file {f.path} (default strategy)",
                )
            if f.flags.get("requires_human_review", False):
                return StrategyDecision(
                    action=DecisionAction.HUMAN_CALL,
                    reason=f"requires_human_review on file {f.path} (default strategy)",
                )
            # Default requires file-level overall confidence to be HIGH
            if f.overall_confidence != Confidence.HIGH:
                return StrategyDecision(
                    action=DecisionAction.HUMAN_CALL,
                    reason=f"overall confidence on {f.path} is {f.overall_confidence.value}, not high (default strategy)",
                )
            # Note: default does NOT gate on per-hunk confidence — that is
            # the distinguishing factor from strict.

        # Require high global overall confidence
        if resolution.overall_confidence != Confidence.HIGH:
            return StrategyDecision(
                action=DecisionAction.HUMAN_CALL,
                reason=f"overall confidence is {resolution.overall_confidence.value}, not high (default strategy)",
            )

        # All checks passed
        return StrategyDecision(
            action=DecisionAction.ACCEPT,
            reason="High confidence, no guardrail concerns (default strategy)",
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

    def _decide_robust(
        self,
        resolution: LLMResolution,
        has_spec_files: bool,
    ) -> StrategyDecision:
        """Robust strategy: same decision logic as default; the orchestrator
        interprets HUMAN_CALL as a signal to drop into the deterministic
        take-theirs fallback rather than write a human call file.

        Implemented as a delegate so commit 1 introduces zero behavior
        change. Commit 3 wires the orchestrator-side branch that treats
        a HUMAN_CALL decision under ROBUST as a take-theirs trigger.
        """
        return self._decide_default(resolution, has_spec_files)
