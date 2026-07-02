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
from .issue_renumber import (
    advance_next_id_to_max,
    format_renumber_trace,
    mask_issue_references,
    rewrite_issue_references,
    rewrite_issue_references_bulk,
    rewrite_references_in_added_lines,
    strip_renumber_traces,
)
from .orchestrator import MergeOrchestrator, MergeReport
from .runtime_sync import (
    DEST_HASH_UNAVAILABLE,
    BypassedCollision,
    IssueMergeRecord,
    RuntimeSyncCollision,
    SyncReport,
    merge_worktree_issues,
    sync_branch_runtime,
)
from .strategy import DecisionAction, StrategyDecider, StrategyDecision
from .version_aggregator import (
    AggregateResult,
    InferResult,
    VersionNotAdvanced,
    aggregate_and_apply,
    infer_branch_bump,
    max_bump,
    read_version_at_ref,
)

__all__ = [
    "advance_next_id_to_max",
    "aggregate_and_apply",
    "AggregateResult",
    "build",
    "BypassedCollision",
    "check_spec_diff",
    "CleanupManager",
    "CleanupReport",
    "Confidence",
    "ConflictContext",
    "ConflictFile",
    "ConflictHunk",
    "ConflictResolver",
    "DecisionAction",
    "DEST_HASH_UNAVAILABLE",
    "FileResolution",
    "format_renumber_trace",
    "GuardrailReport",
    "GuardrailViolation",
    "HumanCallWriter",
    "HunkResolution",
    "InferResult",
    "infer_branch_bump",
    "IssueMergeRecord",
    "merge_worktree_issues",
    "LLMResolution",
    "mask_issue_references",
    "max_bump",
    "MergeGuardrailsCheck",
    "MergeOrchestrator",
    "MergeReport",
    "MergeStrategy",
    "read_version_at_ref",
    "rewrite_issue_references",
    "rewrite_issue_references_bulk",
    "rewrite_references_in_added_lines",
    "RuntimeSyncCollision",
    "StrategyDecider",
    "StrategyDecision",
    "strip_renumber_traces",
    "sync_branch_runtime",
    "SyncReport",
    "VersionNotAdvanced",
]
