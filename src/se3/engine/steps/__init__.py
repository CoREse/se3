"""Step handlers for the flow engine.

Each step type has a handler function that executes the step's logic.
Handlers are registered with the state machine and called during flow execution.
"""

import logging

from ..models import StepType
from .adjudicate import adjudicate_handler
from .analyze import analyze_handler
from .charter_freshness import charter_freshness_handler
from .commit import commit_handler
from .confirm import confirm_handler
from .discovery import discovery_handler
from .implement import implement_handler
from .invariant_check import invariant_check_handler
from .plan import plan_handler
from .plan_tasks import plan_tasks_handler
from .project_summary import project_summary_handler
from .self_check import self_check_handler
from .summarize import summarize_handler
from .test import test_handler
from .version_analyze import version_analyze_handler

logger = logging.getLogger(__name__)


def propose_stub_handler(step, flow):
    """Stub handler for deprecated PROPOSE step type.

    Forwards to plan_handler so old persisted flows can resume without crashing.
    """
    logger.warning(
        "Step type PROPOSE is deprecated. Forwarding to unified plan_handler. "
        "Flow %s, step %s", flow.flow_id, step.step_id
    )
    return plan_handler(step, flow)


def design_stub_handler(step, flow):
    """Stub handler for deprecated DESIGN step type.

    Forwards to plan_handler so old persisted flows can resume without crashing.
    """
    logger.warning(
        "Step type DESIGN is deprecated. Forwarding to unified plan_handler. "
        "Flow %s, step %s", flow.flow_id, step.step_id
    )
    return plan_handler(step, flow)


def project_summary_stub_handler(step, flow):
    """Stub handler for deprecated PROJECT_SUMMARY step type.

    PROJECT_SUMMARY has been merged into ANALYZE. This stub forwards to
    project_summary_handler so old persisted flows can resume without crashing.
    """
    logger.warning(
        "Step type PROJECT_SUMMARY is deprecated (merged into ANALYZE). "
        "Forwarding to project_summary_handler. Flow %s, step %s",
        flow.flow_id, step.step_id,
    )
    return project_summary_handler(step, flow)


# Registry of all step handlers for the state machine
STEP_HANDLERS = {
    StepType.DISCOVERY: discovery_handler,
    StepType.ANALYZE: analyze_handler,
    StepType.PROJECT_SUMMARY: project_summary_stub_handler,  # Deprecated: merged into ANALYZE
    StepType.PLAN: plan_handler,
    StepType.PROPOSE: propose_stub_handler,  # Backward compat for persisted flows
    StepType.DESIGN: design_stub_handler,  # Backward compat for persisted flows
    StepType.PLAN_TASKS: plan_tasks_handler,  # Backward compat for persisted flows
    StepType.CONFIRM: confirm_handler,
    StepType.IMPLEMENT: implement_handler,
    StepType.TEST: test_handler,
    StepType.SELF_CHECK: self_check_handler,
    StepType.ADJUDICATE: adjudicate_handler,
    StepType.INVARIANT_CHECK: invariant_check_handler,
    StepType.CHARTER_FRESHNESS: charter_freshness_handler,
    StepType.VERSION_ANALYZE: version_analyze_handler,
    StepType.COMMIT: commit_handler,
    StepType.SUMMARIZE: summarize_handler,
}

__all__ = [
    "discovery_handler",
    "adjudicate_handler",
    "analyze_handler",
    "project_summary_handler",
    "project_summary_stub_handler",
    "plan_handler",
    "propose_stub_handler",
    "design_stub_handler",
    "plan_tasks_handler",
    "implement_handler",
    "test_handler",
    "self_check_handler",
    "invariant_check_handler",
    "charter_freshness_handler",
    "version_analyze_handler",
    "commit_handler",
    "summarize_handler",
    "STEP_HANDLERS",
]
