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
    ItemMeta,
    load_or_build,
    get_or_build_index,
)
from .spec_loader import (
    LoadResult,
    load_for_step,
    load_full,
)
from .event_stream import (
    Event,
    EventType,
    EventEmitter,
    new_event,
)
from .sink import (
    Sink,
    CliSink,
    JsonSink,
)

# ``EventStream`` is the public name for the in-memory pub/sub emitter that
# ``se3 run`` drives; ``EventEmitter`` is its implementation class.
EventStream = EventEmitter

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
    "ItemMeta",
    "load_or_build",
    "get_or_build_index",
    "LoadResult",
    "load_for_step",
    "load_full",
    "Event",
    "EventType",
    "EventEmitter",
    "EventStream",
    "new_event",
    "Sink",
    "CliSink",
    "JsonSink",
]