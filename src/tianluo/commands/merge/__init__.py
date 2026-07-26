"""Foundation infrastructure for the SE3 merge subsystem.

Modules in this package provide typed Result models, failure-reason
enums, concurrency locking, per-LLM-call tracing, secret redaction, and
post-condition assertions consumed by the merge orchestrator and its
constituent steps. All modules in this package are intentionally
lightweight (no heavy git/LLM dependencies at import time) so that they
can be reused by both the CLI entry points and the engine internals.
"""

from .failure_reason import FailureReason, from_legacy_string, to_legacy_string
from .llm_trace import LLMCallRecord, LLMTrace
from .merge_lock import (
    MergeLock,
    MergeLockBusy,
    MergeLockStale,
    acquire_merge_lock,
)
from .postcondition import (
    PostConditionViolated,
    assert_branch_merged,
    assert_head_is_merge_commit,
    assert_version_bumped,
    check_all,
)
from .result_model import MergeOutcome, MergeReport
from .secret_redact import RedactConfig, SecretRedactor, redact_diff, redact_text

__all__ = [
    "FailureReason",
    "LLMCallRecord",
    "LLMTrace",
    "MergeLock",
    "MergeLockBusy",
    "MergeLockStale",
    "MergeOutcome",
    "MergeReport",
    "PostConditionViolated",
    "RedactConfig",
    "SecretRedactor",
    "acquire_merge_lock",
    "assert_branch_merged",
    "assert_head_is_merge_commit",
    "assert_version_bumped",
    "check_all",
    "from_legacy_string",
    "redact_diff",
    "redact_text",
    "to_legacy_string",
]
