"""merge_integrate step handler — thin adapter over the ``integrate()`` library.

Background: an isolated ``--worktree`` run's release point is the merge, not its
own commit (see version_intent / reconcile). So a worktree flow's step sequence
is extended with two merge-side steps; this handler is the first of them. It
performs the *integration* half — the actual branch merge (with LLM conflict
resolution), runtime sync, issue renumber, and post-condition checks — by
delegating to :func:`se3.engine.merge.integrate`. Everything hard (the
invariants: merge lock, runtime sync, renumber, post-conditions) lives in the
library; this handler is only flow-control glue.

Two boundaries the split buys us (see the group design):

  * **integrate is expensive; its failure mode is "cannot be merged".** When it
    fails, this step fails and the flow stops BEFORE ``version_reconcile`` — no
    version is decided for work that never landed on master. A resume re-runs
    the merge (integrate is safe to re-attempt: an already-merged branch reports
    already-ancestor).

  * **Execution context is the MAIN checkout, inside the merge lock.** This step
    carries a ``cwd`` override (the main checkout); the state machine acquires
    the merge lock and runs it there, not in the worktree the flow body used.
    ``acquire_lock=False`` is passed because the state machine already holds it.

The merged branch is deliberately NOT deleted here (``delete_merged=False``):
``version_reconcile`` still needs to run against master afterward, and the
worktree the flow is executing in must survive until the flow's own state has
been persisted. Worktree archival/branch deletion is a post-flow concern.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ..models import FlowInstance, Step, StepStatus

logger = logging.getLogger(__name__)


def _resolve_merge_root(step: Step, flow: FlowInstance) -> Path:
    """Resolve the main-checkout root this merge step operates on.

    Prefers the step's ``cwd`` override (the main checkout, set when the merge
    steps are appended to a worktree flow). Falls back to resolving the main
    repo root from the flow's recorded ``project_root`` when no override is
    present (defensive — a worktree flow always sets ``cwd``).
    """
    if step.cwd:
        return Path(step.cwd)
    root = flow.state.context.get("project_root") if flow.state else None
    base = Path(root) if root else Path.cwd()
    try:
        from ...config import _resolve_main_repo_root

        return Path(_resolve_main_repo_root(base))
    except Exception:  # noqa: BLE001 - never let root resolution abort the step
        return base


def merge_integrate_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Integrate the flow's isolation branch into master.

    Reads the branch to merge from ``flow.worktree_branch`` and delegates to
    :func:`se3.engine.merge.integrate` against the main checkout. Records the
    structured :class:`MergeResult` on ``step.outputs`` and maps it to a step
    status:

    * success → COMPLETED (the flow may proceed to ``version_reconcile``);
    * pending-human / failure → FAILED, so ``version_reconcile`` never runs for
      work that did not land. The run loop then offers the operator the usual
      retry / abort decision.
    """
    from ..merge import integrate

    project_root = _resolve_merge_root(step, flow)

    branch = flow.worktree_branch or step.inputs.get("worktree_branch")
    if not branch:
        step.status = StepStatus.FAILED
        step.error_message = (
            "merge_integrate: no isolation branch recorded on the flow; cannot "
            "merge back to master."
        )
        logger.error(step.error_message)
        return StepStatus.FAILED

    logger.info(
        "merge_integrate: merging branch %s into master at %s",
        branch,
        project_root,
    )

    # The state machine holds the merge lock for the duration of this step's
    # cwd-override execution, so integrate must NOT re-acquire it (that would
    # deadlock on the same-process blocking flock). delete_merged=False: the
    # branch/worktree stay until reconcile has run and the flow state persisted.
    result = integrate(
        project_root,
        [branch],
        delete_merged=False,
        acquire_lock=False,
    )

    step.outputs["merge_result"] = _summarize_result(result)
    step.outputs["merged_branches"] = list(
        getattr(result, "merged_branches", []) or []
    )
    pending_human = bool(getattr(result, "pending_human", False))
    step.outputs["pending_human"] = pending_human

    if getattr(result, "success", False) and not pending_human:
        step.status = StepStatus.COMPLETED
        return StepStatus.COMPLETED

    # Failure (or a human-escalation the caller must resolve). Surface it as a
    # FAILED step so version_reconcile is not reached — an unmerged branch has
    # no final version to decide.
    reason = getattr(result, "failure_reason", None)
    detail = getattr(result, "failure_detail", None)
    if pending_human and not detail:
        detail = (
            "integrate() escalated to a human (unresolved merge conflict). "
            "Resolve the conflict, then resume to re-run merge_integrate."
        )
    step.status = StepStatus.FAILED
    step.error_message = (
        f"merge_integrate failed to merge branch {branch}: "
        f"{reason or 'unknown'}"
        + (f" — {detail}" if detail else "")
    )
    logger.error(step.error_message)
    return StepStatus.FAILED


def _summarize_result(result: object) -> dict:
    """Extract a JSON-safe summary of the MergeResult for step.outputs/history."""
    keys = (
        "success",
        "failure_reason",
        "failure_detail",
        "merged_branches",
        "newly_merged_branches",
        "already_ancestor_branches",
        "pending_human",
    )
    summary: dict = {}
    for key in keys:
        value = getattr(result, key, None)
        # FailureReason enums / paths etc. → str for a JSON-safe record.
        if value is not None and not isinstance(value, (str, int, float, bool, list)):
            value = str(value)
        summary[key] = value
    return summary
