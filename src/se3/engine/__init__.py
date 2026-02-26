"""SE3 Flow Engine - State machine driven workflow engine.

The flow engine is the core of SE3 3.0, replacing prompt-driven workflows
with a program-controlled state machine. Each state corresponds to a workflow
step, and transitions are controlled by program logic, not LLM decisions.
"""

from .models import Step, StepType, StepStatus, State, Transition, FlowInstance, FlowStatus
from .state_machine import StateMachine
from .persistence import PersistenceManager
from .llm_caller import LLMCaller, LLMCallError
from .schema import FlowInstanceSchema, ContextSchema, build_context_from_flow
from .logging_config import StructuredLogger, LogEventType, LogLevel, get_logger
from .spec_index import (
    SpecIndex,
    SpecInfo,
    get_or_build_index,
    match_specs_for_task,
    assess_change_size,
)

__all__ = [
    "Step",
    "StepType",
    "StepStatus",
    "State",
    "Transition",
    "FlowInstance",
    "FlowStatus",
    "StateMachine",
    "PersistenceManager",
    "LLMCaller",
    "LLMCallError",
    "FlowInstanceSchema",
    "ContextSchema",
    "build_context_from_flow",
    "StructuredLogger",
    "LogEventType",
    "LogLevel",
    "get_logger",
    "SpecIndex",
    "SpecInfo",
    "get_or_build_index",
    "match_specs_for_task",
    "assess_change_size",
]