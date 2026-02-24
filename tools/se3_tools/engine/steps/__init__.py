"""Step handlers for the flow engine.

Each step type has a handler function that executes the step's logic.
Handlers are registered with the state machine and called during flow execution.
"""

from ..models import StepType
from .analyze import analyze_handler
from .commit import commit_handler
from .design import design_handler
from .implement import implement_handler
from .plan_tasks import plan_tasks_handler
from .propose import propose_handler
from .read_spec import read_spec_handler
from .summarize import summarize_handler
from .test import test_handler
from .update_spec import update_spec_handler
from .verify_spec import verify_spec_handler

# Registry of all step handlers for the state machine
STEP_HANDLERS = {
    StepType.ANALYZE: analyze_handler,
    StepType.READ_SPEC: read_spec_handler,
    StepType.PROPOSE: propose_handler,
    StepType.DESIGN: design_handler,
    StepType.PLAN_TASKS: plan_tasks_handler,
    StepType.IMPLEMENT: implement_handler,
    StepType.TEST: test_handler,
    StepType.VERIFY_SPEC: verify_spec_handler,
    StepType.UPDATE_SPEC: update_spec_handler,
    StepType.COMMIT: commit_handler,
    StepType.SUMMARIZE: summarize_handler,
}

__all__ = [
    "analyze_handler",
    "read_spec_handler",
    "propose_handler",
    "design_handler",
    "plan_tasks_handler",
    "implement_handler",
    "test_handler",
    "verify_spec_handler",
    "update_spec_handler",
    "commit_handler",
    "summarize_handler",
    "STEP_HANDLERS",
]
