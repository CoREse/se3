"""Typed result models for the merge orchestrator.

Replaces the legacy ``MergeReport`` dataclass (defined in
``orchestrator.py``) with a split-model design that separates:

  * per-branch outcomes (success / failure / skipped),
  * typed failure reasons (:class:`FailureReason`),
  * semantic field names that distinguish ``newly_merged`` from
    ``already_merged_branches`` and ``with_warnings``.

The legacy ``MergeReport`` remains importable from its original
location for backward compatibility during the deprecation window.
New code SHOULD import from this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields as dataclass_fields
from pathlib import Path
from typing import Any, Optional

from .failure_reason import FailureReason


@dataclass
class EvidenceRecord:
    """Typed evidence payload attached to a guardrail violation.

    The legacy code attached a ``dict`` to ``GuardrailViolation.evidence``
    which made field typos invisible at construction time —
    ``evidence={"strng_line": "..."}`` would silently produce a
    violation with a missing strong-line message downstream.

    H4: This dataclass enumerates every recognised evidence field and
    fails fast at construction (``TypeError`` from the dataclass
    constructor) when an unknown keyword is supplied.  Every field is
    optional so a single class can carry evidence for any violation
    type — see the ``violation_type`` discriminator on
    :class:`GuardrailViolation`.

    Backward-compatibility: :meth:`to_dict` serialises the record back
    to a plain dict (omitting ``None``-valued fields) so existing
    consumers that rely on dict-style access (e.g.
    ``evidence.get("strong_line")``) continue to work without changes.
    """

    # --- Pairing evidence (WEAKENING) ---
    strong_line: Optional[str] = None
    weak_line: Optional[str] = None
    strong_line_no: Optional[int] = None
    weak_line_no: Optional[int] = None
    pairing_score: Optional[float] = None
    prefix_score: Optional[float] = None
    all_pairings: Optional[list[dict]] = None

    # --- Deletion evidence (DELETE) ---
    deleted_line: Optional[str] = None
    deleted_line_no: Optional[int] = None
    when_clause: Optional[str] = None
    when_clauses: Optional[list[str]] = None

    # --- Branch / detector context ---
    branch_name: Optional[str] = None
    trigger_branch: Optional[str] = None
    branch_kind: Optional[str] = None  # e.g. "primary", "corner-case"

    # --- Topology evidence (CHECK_FAILURE from H1/H2) ---
    pre_sha: Optional[str] = None
    post_sha: Optional[str] = None
    parent_count: Optional[int] = None
    min_parents: Optional[int] = None
    topology_check: Optional[str] = None  # "ancestry" | "parent_count"

    # --- Incomplete-check evidence (H5) ---
    exception_type: Optional[str] = None
    exception_msg: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict, omitting fields whose value is ``None``.

        Designed to round-trip with :meth:`from_dict`; consumers that
        store the dict in JSON (call files, log entries) will see only
        the populated keys.
        """
        result: dict[str, Any] = {}
        for f in dataclass_fields(self):
            value = getattr(self, f.name)
            if value is not None:
                result[f.name] = value
        return result

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> Optional["EvidenceRecord"]:
        """Build an :class:`EvidenceRecord` from a dict.

        Unknown keys are NOT silently dropped — they become a
        ``TypeError`` from the dataclass constructor, which is the H4
        fail-fast behaviour the spec calls for.

        Returns ``None`` when *data* is ``None`` or empty so callers
        can transparently handle the no-evidence case.
        """
        if not data:
            return None
        return cls(**data)


@dataclass
class MergeOutcome:
    """Outcome for a single branch merge attempt.

    Each branch processed by the orchestrator produces exactly one
    ``MergeOutcome``.  The outcome is terminal — once the orchestrator
    moves to the next branch the previous outcome is frozen.
    """

    branch: str
    success: bool = False
    # Typed failure reason.  ``None`` means "no failure" (success or
    # no-op).  ``FailureReason.PENDING_HUMAN`` means the merge was
    # paused awaiting operator input.
    failure_reason: Optional[FailureReason] = None
    # Optional human-readable detail string (e.g. stderr from a failed
    # git command, diagnostic text from the LLM caller).
    failure_detail: Optional[str] = None
    # Whether the branch was already an ancestor of HEAD before the
    # merge attempt (a no-op).  Distinct from ``success`` because a
    # no-op does not produce a new merge commit.
    already_ancestor: bool = False
    # Whether the merge succeeded but the guardrails check reported
    # warnings that were repaired (fast mode) or accepted (default).
    warnings_repaired: bool = False
    # SHA of the merge commit produced for this branch, or ``None`` if
    # no merge commit was created (failure, no-op, or pending-human).
    merge_commit_sha: Optional[str] = None
    # Per-branch runtime-sync collisions (lenient mode bypasses).
    runtime_sync_collisions: list = field(default_factory=list)
    # Whether the branch was deleted during cleanup.
    branch_deleted: bool = False


@dataclass
class MergeReport:
    """Result of a merge orchestration run.

    This is the **typed successor** to the legacy
    ``orchestrator.MergeReport``.  Key differences:

      * ``merged_branches`` is replaced by three semantically distinct
        lists: ``newly_merged``, ``already_merged_branches``,
        ``merged_with_warnings``.
      * ``failure_reason`` is a :class:`FailureReason` enum instead of
        a bare string.
      * ``outcomes`` preserves the per-branch result so callers can
        inspect individual branch fates without re-parsing strings.
      * ``call_file`` is ``None``-safe: when ``None`` the field is
        omitted from string representations rather than rendering as
        ``"Call file: None"``.
    """

    success: bool = False

    # --- Branch outcome buckets (semantic split of legacy merged_branches) ---
    # Branches that produced a new merge commit and passed all checks.
    newly_merged: list[str] = field(default_factory=list)
    # Branches that were already ancestors of HEAD (no-op, no commit).
    already_merged_branches: list[str] = field(default_factory=list)
    # Branches that merged but had guardrail warnings that were
    # repaired or accepted (still a success, but flagged).
    merged_with_warnings: list[str] = field(default_factory=list)
    # The branch whose merge failed, if any.
    failed_branch: Optional[str] = None
    # Branches never attempted because an earlier branch failed and
    # halted the sequence.
    unattempted_branches: list[str] = field(default_factory=list)

    # --- Failure reason (typed) ---
    failure_reason: Optional[FailureReason] = None
    failure_detail: Optional[str] = None

    # --- Human escalation ---
    pending_human: bool = False
    human_call_file: Optional[Path] = None

    # --- Per-branch detailed outcomes ---
    outcomes: list[MergeOutcome] = field(default_factory=list)

    # --- Version aggregation ---
    pre_merge_version: Optional[str] = None
    effective_pre_merge_version: Optional[str] = None
    final_version: Optional[str] = None
    bump_type: Optional[str] = None
    version_aggregation_skipped: bool = False
    version_aggregation_error: Optional[str] = None

    # --- Logging ---
    log_file: Optional[Path] = None

    # --- Cleanup ---
    cleanup_report: Optional = None  # type: ignore[type-arg]
    cleanup_skipped: bool = True

    # --- Runtime sync ---
    runtime_sync_skipped_branches: list[str] = field(default_factory=list)
    runtime_sync_skipped_files: list[tuple[str, list[str]]] = field(
        default_factory=list
    )
    runtime_sync_discarded: list[tuple[str, list[str]]] = field(
        default_factory=list
    )
    runtime_sync_collisions: list = field(default_factory=list)
    runtime_sync_collision_path: Optional[str] = None
    runtime_sync_idempotent_bypasses: list[tuple[str, int]] = field(
        default_factory=list
    )
    runtime_sync_idempotent_records: list = field(default_factory=list)

    # --- Rollback state ---
    rollback_failed: bool = False

    def add_outcome(self, outcome: MergeOutcome) -> None:
        """Record a branch outcome and update the summary buckets."""
        self.outcomes.append(outcome)
        if outcome.already_ancestor:
            self.already_merged_branches.append(outcome.branch)
        elif outcome.success:
            if outcome.warnings_repaired:
                self.merged_with_warnings.append(outcome.branch)
            else:
                self.newly_merged.append(outcome.branch)
        elif not outcome.failure_reason or outcome.failure_reason is FailureReason.PENDING_HUMAN:
            # Pending-human is not a failure in the traditional sense but
            # also not a success. It sits in its own state.
            pass

    @property
    def all_merged_branches(self) -> list[str]:
        """All branches that were either newly merged or already ancestors.

        This is the **backward-compatible** view for callers that
        expect a single ``merged_branches`` list.  New code should
        prefer the semantic buckets.
        """
        return self.newly_merged + self.already_merged_branches + self.merged_with_warnings

    @property
    def merged_branches(self) -> list[str]:
        """Deprecated alias for ``all_merged_branches``."""
        return self.all_merged_branches

    @property
    def call_file_str(self) -> Optional[str]:
        """Human-call file path as a string, or ``None`` (never ``"None"``).

        Prevents the legacy ``"Call file: None"`` rendering bug.
        """
        if self.human_call_file is None:
            return None
        return str(self.human_call_file)

    def to_legacy_dict(self) -> dict:
        """Serialize to a dict matching the legacy ``orchestrator.MergeReport`` shape.

        Useful for JSON persistence, test fixtures, and log consumers
        that have not yet migrated to the typed model.
        """
        from .failure_reason import to_legacy_string

        result: dict = {
            "success": self.success,
            "merged_branches": self.all_merged_branches,
            "newly_merged": self.newly_merged,
            "already_merged_branches": self.already_merged_branches,
            "merged_with_warnings": self.merged_with_warnings,
            "failed_branch": self.failed_branch,
            "failure_reason": to_legacy_string(self.failure_reason) or "",
            "failure_detail": self.failure_detail,
            "pending_human": self.pending_human,
            "human_call_file": str(self.human_call_file) if self.human_call_file else None,
            "log_file": str(self.log_file) if self.log_file else None,
            "pre_merge_version": self.pre_merge_version,
            "effective_pre_merge_version": self.effective_pre_merge_version,
            "final_version": self.final_version,
            "bump_type": self.bump_type,
            "version_aggregation_skipped": self.version_aggregation_skipped,
            "version_aggregation_error": self.version_aggregation_error,
            "cleanup_skipped": self.cleanup_skipped,
            "runtime_sync_skipped_branches": self.runtime_sync_skipped_branches,
            "runtime_sync_skipped_files": self.runtime_sync_skipped_files,
            "runtime_sync_discarded": self.runtime_sync_discarded,
            "runtime_sync_collision_path": self.runtime_sync_collision_path,
            "runtime_sync_idempotent_bypasses": self.runtime_sync_idempotent_bypasses,
            "rollback_failed": self.rollback_failed,
            "unattempted_branches": self.unattempted_branches,
            "outcomes": [
                {
                    "branch": o.branch,
                    "success": o.success,
                    "failure_reason": to_legacy_string(o.failure_reason) or "",
                    "failure_detail": o.failure_detail,
                    "already_ancestor": o.already_ancestor,
                    "warnings_repaired": o.warnings_repaired,
                    "merge_commit_sha": o.merge_commit_sha,
                }
                for o in self.outcomes
            ],
        }
        if self.cleanup_report is not None:
            from dataclasses import asdict

            result["cleanup_report"] = asdict(self.cleanup_report)
        return result
