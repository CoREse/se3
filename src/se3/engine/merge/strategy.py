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
    - strict:  all hunks high confidence + guardrails pass → ACCEPT,
               otherwise HUMAN_CALL
    - fast:    regular files → aggressive accept (low confidence ok),
               spec files → same as default (spec_guardrail_concern → HUMAN_CALL)
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
            return self._decide_fast(resolution, has_spec_files)
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
        """Strict strategy: ALL hunks high confidence + no spec concerns → ACCEPT."""
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
        has_spec_files: bool,
    ) -> StrategyDecision:
        """Fast strategy: aggressive for regular files, spec files = default.

        For spec files, spec_guardrail_concern still forces HUMAN_CALL.
        For regular files, accept even low confidence unless explicitly flagged.
        """
        # spec_guardrail_concern is NEVER ignored, even in fast mode
        if resolution.flags.get("spec_guardrail_concern", False):
            return StrategyDecision(
                action=DecisionAction.HUMAN_CALL,
                reason="spec_guardrail_concern flag set (fast strategy — spec files never exempt)",
            )

        # Check per-file spec guardrail concerns
        spec_file_concerns = []
        for f in resolution.files:
            if f.is_spec and f.flags.get("spec_guardrail_concern", False):
                spec_file_concerns.append(f.path)

        if spec_file_concerns:
            return StrategyDecision(
                action=DecisionAction.HUMAN_CALL,
                reason=f"spec_guardrail_concern on spec file(s): {', '.join(spec_file_concerns)} (fast strategy)",
            )

        # For spec files: same as default (requires high confidence, no guardrail concerns).
        # For regular (non-spec) files: fast mode aggressively accepts even low confidence
        # (per-file requires_human_review on regular files is intentionally ignored).
        has_spec_with_low_confidence = False
        for f in resolution.files:
            if f.is_spec:
                # Spec files in fast mode still need careful handling
                if f.flags.get("requires_human_review", False):
                    return StrategyDecision(
                        action=DecisionAction.HUMAN_CALL,
                        reason=f"requires_human_review on spec file {f.path} (fast strategy)",
                    )
                if f.overall_confidence != Confidence.HIGH:
                    has_spec_with_low_confidence = True

        if has_spec_with_low_confidence:
            return StrategyDecision(
                action=DecisionAction.HUMAN_CALL,
                reason="Low confidence on spec file(s) (fast strategy)",
            )

        # All checks passed — accept (even low confidence on regular files)
        return StrategyDecision(
            action=DecisionAction.ACCEPT,
            reason="Fast strategy: regular files accepted, spec files pass guardrails",
        )
