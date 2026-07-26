"""version_reconcile step handler — thin adapter over the ``reconcile()`` library.

The second merge-side step of a worktree flow. Where the old design baked the
version decision into the session's own commit (against a stale pre-session
baseline — the cause of the 11.12.0 collision), this step re-derives the FINAL
version at merge time, against master's CURRENT version, by delegating to
:func:`tianluo.engine.merge.reconcile`.

Key properties inherited from the library core (nothing re-implemented here):

  * **Unconditional.** ``reconcile()`` has no trigger predicate; it runs on the
    already-ancestor / no-op-merge path too. When there are no outstanding
    intents it is a clean success no-op — this handler still COMPLETES.
  * **Idempotent.** Consumed intents are marked and the reconcile commit carries
    a git-durable trailer, so a resume that re-enters this step (its failure
    mode is "version computed wrong", recovered by re-running ONLY the version
    decision) never double-bumps.
  * **Two channels.** Deterministic SemVer (no LLM) by default; the custom
    ``tianluo/version-rules.md`` LLM channel otherwise. This handler passes
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

from ..models import FlowInstance, Step, StepStatus, StepType

logger = logging.getLogger(__name__)


def _resolve_merge_root(step: Step, flow: FlowInstance) -> Path:
    """Resolve the main-checkout root reconcile operates on (see merge_integrate).

    Strict, fail-loud resolution when no ``cwd`` override is present: a genuine
    main-repo probe fault raises :class:`MergeCheckoutResolutionError` rather than
    silently degrading to the flow's base path (which could be the isolation
    worktree, where reconcile would write the version/changelog outside master).
    A ``None`` probe result is the legitimate "base IS the main checkout" case.
    """
    if step.cwd:
        return Path(step.cwd)
    from ...config import MainRepoProbeError, probe_main_repo_root
    from ..state_machine import MergeCheckoutResolutionError

    root = flow.state.context.get("project_root") if flow.state else None
    base = Path(root) if root else Path.cwd()
    try:
        main = probe_main_repo_root(base)
    except MainRepoProbeError as exc:
        raise MergeCheckoutResolutionError(
            f"version_reconcile step has no cwd override and the main checkout "
            f"could not be resolved from {base}; refusing to reconcile in the "
            f"isolation worktree: {exc}"
        ) from exc
    return main if main is not None else base


def version_reconcile_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Derive and apply the final project version at merge time.

    Delegates to :func:`tianluo.engine.merge.reconcile` against the main checkout,
    records the structured :class:`ReconcileResult` on ``step.outputs``, and
    maps it to a step status. A genuine reconcile fault (unparseable version,
    regression/collision, write/commit failure) surfaces as FAILED so a resume
    re-runs only the version decision (the merge from merge_integrate stands).
    """
    from ..merge import reconcile
    from ..merge.reconcile import ReconcileError, undo_last_reconcile
    from ..state_machine import MergeCheckoutResolutionError
    from ..version_intent import IntentReadError, reconcile_commit_exists

    try:
        project_root = _resolve_merge_root(step, flow)
    except MergeCheckoutResolutionError as exc:
        step.status = StepStatus.FAILED
        step.error_message = str(exc)
        logger.error(step.error_message)
        return StepStatus.FAILED

    logger.info("version_reconcile: reconciling version at %s", project_root)

    # No-version-intent-BY-DESIGN completion. A worktree flow whose task type has
    # no VERSION_ANALYZE step (e.g. 'review': ANALYZE / INVARIANT_CHECK /
    # SUMMARIZE) emits no version intent and lands no commits — its merge_integrate
    # is a no-op already-ancestor merge. The merge steps are still appended (the
    # no-COMMIT fallback in append_worktree_merge_steps tacks them on the tail), so
    # this handler runs, but a flow-scoped reconcile() would hard-fault on the
    # unaccounted flow_id (no intent JSON, no reconcile commit) and fail the whole
    # flow at its final step even though the review completed successfully. That
    # scoped hard-fault exists to catch a DROPPED intent for a flow that SHOULD
    # have emitted one — not a flow that never had a version_analyze step. Treat
    # the by-design no-intent case as a real completion (nothing to land, nothing
    # to version), exactly as the version-disabled case below completes with no
    # reconcile commit.
    # The authoritative post-analyze sequence is ``selected_steps``; when it is not
    # populated (e.g. a flow reconstructed for a resume) fall back to the task
    # type's default sequence so we only ever short-circuit on a POSITIVE "this flow
    # never had a version_analyze step" signal — never on a merely-empty sequence,
    # which would mask a genuinely dropped intent (that must still FAIL below).
    selected = getattr(flow.state, "selected_steps", None) if flow.state else None
    if selected:
        sequence_steps = selected
    else:
        from ..models import get_default_step_sequence

        sequence_steps = get_default_step_sequence(
            getattr(flow, "task_type", "feature") or "feature"
        )
    emits_version_intent = StepType.VERSION_ANALYZE in sequence_steps
    if not emits_version_intent:
        logger.info(
            "version_reconcile: flow %s has no version_analyze step (no version "
            "intent by design); completing with nothing to reconcile",
            flow.flow_id,
        )
        step.outputs["reconcile_result"] = {
            "success": True,
            "base_version": None,
            "final_version": None,
            "bump_type": None,
            "channel": "noop",
            "consumed_flow_ids": [],
            "reconcile_commit": None,
            "is_tag": False,
            "tag_name": None,
            "tag_created": False,
            "already_reconciled": True,
        }
        step.outputs["final_version"] = None
        step.outputs["channel"] = "noop"
        step.outputs["is_tag"] = False
        step.outputs["tag_name"] = None
        step.outputs["tag_created"] = False
        step.status = StepStatus.COMPLETED
        return StepStatus.COMPLETED

    # Human-review revision path (the confirmation gate rejected the version):
    # the prior run already created a durable reconcile commit, so a plain re-run
    # would see the git-durable trailer and no-op, leaving the rejected version
    # standing. Undo that commit first (safe while it is still HEAD — the CONFIRM
    # gate creates no commit) so the intents become outstanding again and
    # reconcile recomputes, honouring the reviewer's feedback in the custom-rules
    # channel.
    revision_feedback: str | None = None
    if step.inputs.get("is_revision"):
        revision_feedback = step.inputs.get("revision_feedback") or None
        try:
            undo = undo_last_reconcile(project_root, flow.flow_id)
            logger.info(
                "version_reconcile: revision re-run; prior reconcile undone=%s",
                undo,
            )
        except ReconcileError as exc:
            step.status = StepStatus.FAILED
            step.error_message = (
                f"version_reconcile: failed to undo the rejected prior "
                f"reconcile: {exc}"
            )
            logger.error(step.error_message)
            return StepStatus.FAILED

        # Undo returning False is NOT safe to ignore on a rejection. It means the
        # reconcile commit is no longer HEAD — the merge lock is released between
        # merge_integrate and version_reconcile, so a concurrent flow's merge can
        # land on master while the reviewer deliberates and bury this flow's
        # reconcile commit. Proceeding would let reconcile() see the still-present
        # git-durable trailer, no-op with already_reconciled=True, and COMPLETE —
        # silently re-releasing the very version the reviewer just rejected. When
        # the rejected commit still exists in history but cannot be undone, FAIL
        # and require manual intervention. (If no reconcile commit exists for this
        # flow at all — e.g. the prior decision was a no-op bump — there is
        # nothing to reject, so a fresh recompute is safe and we fall through.)
        if not undo and reconcile_commit_exists(project_root, flow.flow_id):
            step.status = StepStatus.FAILED
            step.error_message = (
                "version_reconcile: the version decision was rejected but its "
                "reconcile commit is no longer HEAD (a concurrent merge landed "
                "on master during review), so it cannot be safely undone for "
                "recompute. Rebase/undo the buried reconcile commit for flow "
                f"{flow.flow_id} manually, then re-run this step."
            )
            logger.error(step.error_message)
            return StepStatus.FAILED

    try:
        # Restrict to THIS flow's own intent (change #3, concurrency guard). The
        # merge lock is released between a worktree flow's merge_integrate and
        # version_reconcile steps (state_machine wraps ONE step at a time), so a
        # second flow's merge_integrate can land its intent on master in that
        # window. An unrestricted sweep would then let whichever flow reconciles
        # first consume BOTH intents under a single max(bump) — two features
        # sharing one version, the exact 2026-07-06 incident. Each flow's release
        # point (its own reconcile) must bump only for its own intent; the other
        # flow's reconcile then bumps independently. Idempotency (reconcile-commit
        # trailer) still makes a resume safe. The unrestricted sweep is reserved
        # for the CLI ``luo merge`` path, which holds the lock across both halves.
        result = reconcile(
            project_root,
            flow_ids=[flow.flow_id],
            revision_feedback=revision_feedback,
        )
    except (ReconcileError, IntentReadError) as exc:
        # IntentReadError is reconcile()'s typed refusal on a persistently faulting
        # reconcile-commit / intent git probe (it deliberately refuses to fail
        # open). The CLI adapter already maps it to a non-zero exit; the in-flow
        # step must likewise map it to a graceful FAILED (not let a raw traceback
        # escape the handler) so the flow stays resumable through the normal retry
        # loop rather than crashing the engine.
        step.status = StepStatus.FAILED
        step.error_message = f"version_reconcile failed: {exc}"
        logger.error(step.error_message)
        return StepStatus.FAILED

    # Missing-intent guard (in-flow step only). This step is scoped to exactly
    # THIS flow, whose intent is the sole carrier of its release decision. A no-op
    # result (already_reconciled) is legitimate ONLY when a durable reconcile
    # commit for this flow already exists (an idempotent resume after the bump
    # landed). A no-op with NO reconcile commit for the flow means the intent file
    # was never committed, was dropped by a bad merge, or was manually removed —
    # collecting nothing, reconcile() bumped nothing. Completing here would land
    # the merged work on master with no version bump and no changelog entry (the
    # exact silent-drop this redesign exists to prevent). Fail so a resume /
    # operator can restore the intent instead of publishing an unversioned merge.
    #
    # EXCEPTION: version bumping disabled for the project. version_analyze emits
    # no intent by design in that mode, so reconcile() legitimately no-ops with no
    # reconcile commit (version_disabled=True). Treating that as a dropped intent
    # would wedge every worktree flow on a version-disabled project. The disabled
    # case is a real completion — no version, no changelog, no reconcile commit —
    # exactly as a non-worktree flow's commit step skips bumping when disabled.
    if (
        result.already_reconciled
        and not result.version_disabled
        and not reconcile_commit_exists(project_root, flow.flow_id)
    ):
        step.status = StepStatus.FAILED
        step.error_message = (
            "version_reconcile: no version intent found for flow "
            f"{flow.flow_id} and no reconcile commit exists — the merged work "
            "would be published with no version bump or changelog entry. The "
            f"intent file (tianluo/version-intents/{flow.flow_id}.json) was not "
            "committed, was dropped by a bad merge, or was removed. Restore it "
            "and re-run this step."
        )
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
        "is_tag": result.is_tag,
        "tag_name": result.tag_name,
        "tag_created": result.tag_created,
        "already_reconciled": result.already_reconciled,
    }
    step.outputs["final_version"] = result.final_version
    step.outputs["base_version"] = result.base_version
    step.outputs["channel"] = result.channel
    step.outputs["is_tag"] = result.is_tag
    step.outputs["tag_name"] = result.tag_name
    step.outputs["tag_created"] = result.tag_created

    step.status = StepStatus.COMPLETED
    return StepStatus.COMPLETED
