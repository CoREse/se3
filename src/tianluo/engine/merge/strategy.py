"""StrategyDecider — Three-strategy decision matrix for conflict resolution.

Implements safe/strict/fast decision logic based on LLM resolution
confidence and the flags the resolver attaches to each file.
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
    - safe:    no requires_human_review flag → ACCEPT, otherwise
               HUMAN_CALL (the legacy ``default`` tier).
    - strict:  short-circuited by orchestrator (_handle_conflict skips LLM
               and goes directly to human call). Retained as fallback.
    - fast:    ACCEPT or REJECT only — never HUMAN_CALL.
               Aggressive accept (low confidence ok); REJECT only on an
               explicit global requires_human_review flag.
    """

    def decide(
        self,
        resolution: LLMResolution,
        strategy: MergeStrategy = MergeStrategy.SAFE,
    ) -> StrategyDecision:
        """Evaluate the LLM resolution and return a decision.

        Args:
            resolution: The LLM's structured resolution output.
            strategy: The conflict resolution strategy.

        Returns:
            StrategyDecision with action and reason.
        """
        if strategy == MergeStrategy.SAFE:
            return self._decide_safe(resolution)
        elif strategy == MergeStrategy.STRICT:
            return self._decide_strict(resolution)
        elif strategy == MergeStrategy.FAST:
            return self._decide_fast(resolution)
        else:
            logger.warning("Unknown strategy %s, falling back to safe", strategy)
            return self._decide_safe(resolution)

    def _decide_safe(
        self,
        resolution: LLMResolution,
    ) -> StrategyDecision:
        """Safe strategy: gate on the explicit human-review flag only.

        Under the LLM-as-editor model (G3), success is observable on
        disk — ``ConflictResolver.resolve`` wraps :meth:`resolve_batch`
        and synthesises a resolution whose ``requires_human_review``
        flag reflects whether the LLM cleared every conflict marker.
        The decider therefore no longer gates on the (now
        informational-only) ``overall_confidence`` rating — that would
        re-introduce the legacy JSON-decision behaviour the new model
        deliberately replaced.  A ``MEDIUM``-confidence resolution that
        cleared every marker is accepted; only flag-bearing resolutions
        escalate to a human MCP call.
        """
        if resolution.flags.get("requires_human_review", False):
            return StrategyDecision(
                action=DecisionAction.HUMAN_CALL,
                reason="requires_human_review flag set (safe strategy)",
            )

        # Check per-file flags only; confidence rating is informational.
        for f in resolution.files:
            if f.flags.get("requires_human_review", False):
                return StrategyDecision(
                    action=DecisionAction.HUMAN_CALL,
                    reason=f"requires_human_review on file {f.path} (safe strategy)",
                )

        # All checks passed
        return StrategyDecision(
            action=DecisionAction.ACCEPT,
            reason="No human-review flags set (safe strategy)",
        )

    def _decide_strict(
        self,
        resolution: LLMResolution,
    ) -> StrategyDecision:
        """Strict strategy: placeholder — orchestrator short-circuits this path.

        In practice, the orchestrator handles strict strategy in
        ``_handle_conflict`` by skipping LLM resolution and writing a human
        call directly. This method is retained as a fallback for unexpected
        code paths (e.g., if the orchestrator's short-circuit is bypassed).
        """
        # Check global flags
        if resolution.flags.get("requires_human_review", False):
            return StrategyDecision(
                action=DecisionAction.HUMAN_CALL,
                reason="requires_human_review flag set (strict strategy)",
            )

        # Check every hunk in every file must be high confidence
        for f in resolution.files:
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
            reason="All hunks high confidence (strict strategy)",
        )

    def _decide_fast(
        self,
        resolution: LLMResolution,
    ) -> StrategyDecision:
        """Fast strategy: aggressive accept; only a global human-review flag rejects.

        Fast mode NEVER returns HUMAN_CALL — only ACCEPT or REJECT.
        - Files are always accepted regardless of confidence.
        - A global ``requires_human_review`` flag REJECTs the whole batch;
          a per-file flag is accepted but logged, since fast mode's contract
          is to never park a merge waiting on a human.
        - Any REJECT is translated by the orchestrator into a clean abort with
          no human call file written.
        """
        # requires_human_review at global level → REJECT (fast never calls human)
        if resolution.flags.get("requires_human_review", False):
            return StrategyDecision(
                action=DecisionAction.REJECT,
                reason="requires_human_review flag set (fast strategy aborts, no human call)",
            )

        # Collect any pathological per-file flags for visibility
        dropped_flags: list[str] = []
        for f in resolution.files:
            if f.flags.get("requires_human_review", False):
                dropped_flags.append(f"requires_human_review on {f.path}")
                logger.warning(
                    "Fast strategy accepted file %s despite per-file "
                    "requires_human_review flag from LLM",
                    f.path,
                )

        reason = "Fast strategy: accepted"
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

    def _run_resolver(
        self,
        resolver: "ConflictResolver",
        conflict_files: list[ConflictFile],
        context: BatchContext,
        max_iterations: int,
    ) -> BatchResolveOutcome:
        """Drive ``ConflictResolver`` and return a :class:`BatchResolveOutcome`.

        Calls :meth:`ConflictResolver.resolve` (the public entry point,
        which wraps ``resolve_batch``) so that test infrastructure that
        monkeypatches ``ConflictResolver.resolve`` keeps working.
        Builds a :class:`ConflictContext` from the supplied
        ``BatchContext`` and ``conflict_files`` to satisfy the wrapper's
        signature, then derives a :class:`BatchResolveOutcome` from the
        resulting :class:`LLMResolution` by scanning the working tree
        for unresolved files.
        """
        from .conflict_resolver import ConflictContext

        ctx = ConflictContext(
            project_root=context.project_root,
            ours_branch=context.ours_branch,
            theirs_branch=context.theirs_branch,
            merge_base=context.merge_base,
            ours_head_sha=context.ours_head_sha,
            ours_head_message=context.ours_head_message,
            theirs_head_sha=context.theirs_head_sha,
            theirs_head_message=context.theirs_head_message,
            ours_log_oneline=list(context.ours_log_oneline),
            theirs_log_oneline=list(context.theirs_log_oneline),
            files=list(conflict_files),
        )
        # Call ``resolve()`` with the positional signature that legacy
        # test mocks expect — ``(self, ctx, strategy)``.  The
        # ``max_iterations`` kwarg is intentionally NOT forwarded: the
        # public wrapper reads the cap from project config when not
        # supplied, and many existing tests monkeypatch ``resolve`` with
        # a callable that only accepts the two positional arguments.
        try:
            resolution = resolver.resolve(
                ctx, strategy=context.strategy, max_iterations=max_iterations,
            )
        except TypeError:
            resolution = resolver.resolve(ctx, context.strategy)

        # Prefer the originating BatchResolveOutcome if ``resolve()``
        # attached one (the production wrapper does this).  This
        # preserves the real iteration count and the resolver's
        # observed-on-disk classification of resolved vs. unresolved
        # paths.  Falls back to re-deriving from the LLMResolution
        # when a test mock returned a hand-built resolution without
        # an attached outcome.
        attached = getattr(resolution, "_batch_outcome", None)
        if attached is not None:
            outcome = attached
        else:
            from .conflict_resolver import _has_conflict_markers

            unresolved: list[Path] = []
            resolved: list[Path] = []
            for fr in resolution.files:
                abs_path = context.project_root / fr.path
                content_has_markers = (
                    fr.resolved_content and _has_conflict_markers(fr.resolved_content)
                )
                flag_unresolved = bool(
                    fr.flags.get("contains_conflict_markers", False)
                    or content_has_markers
                )
                if flag_unresolved:
                    unresolved.append(abs_path)
                else:
                    resolved.append(abs_path)

            outcome = BatchResolveOutcome(
                resolved=resolved,
                unresolved=unresolved,
                iterations_used=1,
            )
            if unresolved:
                outcome.escalation_reason = (
                    "strict_to_human"
                    if context.strategy == MergeStrategy.STRICT
                    else "fast_failed"
                )
        # Stash the synthesised resolution on the outcome (under a
        # private attribute) so the orchestrator can reuse it for
        # downstream consumers (human-call writer) without re-synthesising.
        outcome._resolution = resolution  # type: ignore[attr-defined]
        return outcome

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
        Additional flag-based gates from :meth:`_decide_fast` are also
        applied after the marker check.
        """
        outcome = self._run_resolver(
            resolver, conflict_files, context, max_iterations,
        )
        if not outcome.success:
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
        # Markers cleared — defer to the legacy flag-based gate for
        # fast strategy (explicit requires_human_review).  It still
        # applies because the LLM-as-editor model trusts the LLM's
        # signals about file quality on top of the on-disk marker scan.
        resolution = getattr(outcome, "_resolution", None)
        if resolution is not None:
            flag_decision = self._decide_fast(resolution)
            if flag_decision.action != DecisionAction.ACCEPT:
                flag_decision.outcome = outcome
                return flag_decision
        return StrategyDecision(
            action=DecisionAction.ACCEPT,
            reason=(
                f"Fast strategy: LLM cleared all conflict markers in "
                f"{outcome.iterations_used} iteration(s)"
            ),
            outcome=outcome,
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
        what to do when the LLM cannot converge.  Flag-based gates from
        :meth:`_decide_safe` (explicit ``requires_human_review``) still
        apply after the marker check.
        """
        outcome = self._run_resolver(
            resolver, conflict_files, context, max_iterations,
        )
        if not outcome.success:
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
        # Markers cleared — defer to the legacy flag-based gates so
        # the safe strategy still escalates resolutions that
        # explicitly request human review.
        resolution = getattr(outcome, "_resolution", None)
        if resolution is not None:
            flag_decision = self._decide_safe(resolution)
            if flag_decision.action != DecisionAction.ACCEPT:
                flag_decision.outcome = outcome
                return flag_decision
        return StrategyDecision(
            action=DecisionAction.ACCEPT,
            reason=(
                f"Safe strategy: LLM cleared all conflict markers in "
                f"{outcome.iterations_used} iteration(s)"
            ),
            outcome=outcome,
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

