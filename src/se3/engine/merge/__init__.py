"""SE3 Merge engine — Sequential branch merging with LLM conflict resolution."""

from .cleanup import CleanupManager, CleanupReport
from .conflict_context import (
    ConflictContext,
    ConflictFile,
    ConflictHunk,
    build,
)
from .conflict_resolver import (
    ConflictResolver,
    Confidence,
    FileResolution,
    HunkResolution,
    LLMResolution,
    MergeStrategy,
)
from .guardrails import (
    GuardrailReport,
    GuardrailViolation,
    MergeGuardrailsCheck,
    check_spec_diff,
)
from .human_call import HumanCallWriter
from .orchestrator import MergeOrchestrator, MergeReport
from .runtime_sync import RuntimeSyncCollision, SyncReport, sync_branch_runtime
from .strategy import DecisionAction, StrategyDecider, StrategyDecision
from .version_aggregator import (
    AggregateResult,
    aggregate_and_apply,
    infer_branch_bump,
    max_bump,
    read_version_at_ref,
)

__all__ = [
    "aggregate_and_apply",
    "AggregateResult",
    "build",
    "check_spec_diff",
    "CleanupManager",
    "CleanupReport",
    "Confidence",
    "ConflictContext",
    "ConflictFile",
    "ConflictHunk",
    "ConflictResolver",
    "DecisionAction",
    "FileResolution",
    "GuardrailReport",
    "GuardrailViolation",
    "HumanCallWriter",
    "HunkResolution",
    "infer_branch_bump",
    "LLMResolution",
    "max_bump",
    "MergeGuardrailsCheck",
    "MergeOrchestrator",
    "MergeReport",
    "MergeStrategy",
    "read_version_at_ref",
    "StrategyDecider",
    "StrategyDecision",
]
