"""version_reconcile step handler — thin adapter over the ``reconcile()`` library.

The second merge-side step of a worktree flow. Where the old design baked the
version decision into the session's own commit (against a stale pre-session
baseline — the cause of the 11.12.0 collision), this step re-derives the FINAL
version at merge time, against master's CURRENT version, by delegating to
:func:`se3.engine.merge.reconcile`.

Key properties inherited from the library core (nothing re-implemented here):

  * **Unconditional.** ``reconcile()`` has no trigger predicate; it runs on the
    already-ancestor / no-op-merge path too. When there are no outstanding
    intents it is a clean success no-op — this handler still COMPLETES.
  * **Idempotent.** Consumed intents are marked and the reconcile commit carries
    a git-durable trailer, so a resume that re-enters this step (its failure
    mode is "version computed wrong", recovered by re-running ONLY the version
    decision) never double-bumps.
  * **Two channels.** Deterministic SemVer (no LLM) by default; the custom
    ``se3/version-rules.md`` LLM channel otherwise. This handler passes
    ``llm_call=None`` so the library builds a real LLMCaller when needed.

Per-step confirmation gate: because this is an ordinary step in the sequence,
a configured ``version_reconcile: {reviewer: human}`` inserts a CONFIRM step
after it through the normal confirmation-insertion machinery — so a human can
gate *only the version decision* without re-gating the (already-completed,
expensive) merge_integrate step.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..models import FlowInstance, Step, StepStatus

logger = logging.getLogger(__name__)


def _resolve_merge_root(step: Step, flow: FlowInstance) -> Path:
    """Resolve the main-checkout root reconcile operates on (see merge_integrate)."""
    if step.cwd:
        return Path(step.cwd)
    root = flow.state.context.get("project_root") if flow.state else None
    base = Path(root) if root else Path.cwd()
    try:
        from ...config import _resolve_main_repo_root

        return Path(_resolve_main_repo_root(base))
    except Exception:  # noqa: BLE001 - never let root resolution abort the step
        return base


def version_reconcile_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Derive and apply the final project version at merge time.

    Delegates to :func:`se3.engine.merge.reconcile` against the main checkout,
    records the structured :class:`ReconcileResult` on ``step.outputs``, and
    maps it to a step status. A genuine reconcile fault (unparseable version,
    regression/collision, write/commit failure) surfaces as FAILED so a resume
    re-runs only the version decision (the merge from merge_integrate stands).
    """
    from ..merge import reconcile
    from ..merge.reconcile import ReconcileError

    project_root = _resolve_merge_root(step, flow)

    logger.info("version_reconcile: reconciling version at %s", project_root)

    try:
        # No flow_ids restriction: reconcile sweeps every outstanding (unconsumed)
        # merged-in intent and re-bases on master's current version. Idempotency
        # (consumed markers + reconcile-commit trailer) makes a resume safe.
        result = reconcile(project_root)
    except ReconcileError as exc:
        step.status = StepStatus.FAILED
        step.error_message = f"version_reconcile failed: {exc}"
        logger.error(step.error_message)
        return StepStatus.FAILED

    step.outputs["reconcile_result"] = {
        "success": result.success,
        "base_version": result.base_version,
        "final_version": result.final_version,
        "bump_type": result.bump_type,
        "channel": result.channel,
        "consumed_flow_ids": list(result.consumed_flow_ids),
        "reconcile_commit": result.reconcile_commit,
        "already_reconciled": result.already_reconciled,
    }
    step.outputs["final_version"] = result.final_version
    step.outputs["base_version"] = result.base_version
    step.outputs["channel"] = result.channel

    step.status = StepStatus.COMPLETED
    return StepStatus.COMPLETED
