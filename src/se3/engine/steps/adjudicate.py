"""Adjudicate step handler — the fix-loop's spec-contradiction 'police'.

The review layer (``self_check`` / ``invariant_check``) reports *deviations*
with high recall; it deliberately holds no authority to rule on a contradiction.
When a task description contradicts itself (or conflicts with a hard
constraint), the fix loop can oscillate: the same location is flagged in
opposite directions across rounds, each fix undoing the last. ADJUDICATE is the
single layer that *rules* on such contradictions.

Its input is the cross-round issue-fingerprint ledger (persisted in
``flow.state.context``) plus the currently-effective ``task_description`` and
``plan`` — never the full transcript. Its product is an **override patch**:
``adjudicated_description`` (overrides ``task_description``) and/or
``adjudicated_plan`` (overrides the latest plan's ``task_groups``), kept minimal,
with rationale and timestamp, stored in this step's own ``outputs`` so the
original discovery/plan outputs stay byte-for-byte untouched.

This module currently holds only the handler skeleton (group G1: infrastructure
and registration). The ledger read/write, trigger evaluation, prompt assembly,
LLM ruling, supersede/abolish bookkeeping, and effective-text routing land in
later groups.
"""

from __future__ import annotations

import logging

from ..models import FlowInstance, Step, StepStatus

logger = logging.getLogger(__name__)


def adjudicate_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Rule on spec contradictions surfaced by the fix loop.

    Skeleton (group G1): returns COMPLETED as a placeholder so the step type is
    registrable and routable. Later groups fill in ledger-driven prompt
    assembly, the LLM ruling, and the override-patch products; a ruling that
    modifies the task description will return PAUSED to route through the
    confirmation gate (``confirmation.steps.adjudicate``, reviewer: human).
    """
    logger.info(
        "ADJUDICATE step %s (flow %s): skeleton handler, no ruling yet",
        step.step_id, flow.flow_id,
    )
    return StepStatus.COMPLETED
