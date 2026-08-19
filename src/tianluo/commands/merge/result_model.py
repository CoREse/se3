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
from typing import TYPE_CHECKING, Any, Optional, Union

from .failure_reason import FailureReason

if TYPE_CHECKING:
    from .cleanup import CleanupReport


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
    # Whether the merge succeeded but a post-merge check reported
    # warnings that were repaired or accepted. No merge phase sets this
    # today; the field and its ``merged_with_warnings`` bucket stay so
    # archived reports that recorded it still round-trip.
    warnings_repaired: bool = False
    # SHA of the merge commit produced for this branch, or ``None`` if
    # no merge commit was created (failure, no-op, or pending-human).
    merge_commit_sha: Optional[str] = None
    # Whether the *git merge step itself* completed successfully, even
    # if a downstream sub-step (runtime sync, version aggregation,
    # post-condition) later failed.  G3 semantic-alignment fix: for
    # branches whose git merge succeeded but whose runtime sync
    # collided (or timed out / OS-errored), the outcome carries
    # ``success=False`` to reflect the overall failure while
    # ``git_merge_succeeded=True`` lets typed-model consumers see that
    # the branch IS in the legacy ``merged_branches`` bucket because
    # git completed its merge cleanly.  Without this field the typed
    # outcome would conflict with the legacy bucket (which records
    # the branch as merged) — operators relying on the typed view
    # would see "branch failed" while the bucket says "merged".
    git_merge_succeeded: bool = False
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
    # Named ``newly_merged_branches`` (not ``newly_merged``) so that the
    # typed model and the legacy ``orchestrator.MergeReport`` share the
    # same field name — the CLI ``_split_merged_buckets`` reads this key
    # via ``getattr`` and would silently see an empty list if the names
    # diverged.
    newly_merged_branches: list[str] = field(default_factory=list)
    # Branches that were already ancestors of HEAD (no-op, no commit).
    # Named ``already_ancestor_branches`` to match the legacy model field.
    already_ancestor_branches: list[str] = field(default_factory=list)
    # Branches whose ``warnings_repaired`` outcome flag was set (still a
    # success, but flagged). Populated only from archived reports — see
    # ``MergeOutcome.warnings_repaired``.
    merged_with_warnings: list[str] = field(default_factory=list)
    # Backward-compatible aggregate (legacy field).  When non-empty, this
    # is returned by the ``merged_branches`` property; otherwise the
    # property falls back to concatenating the semantic buckets.
    merged_branches: list[str] = field(default_factory=list)
    # The branch whose merge failed, if any.
    failed_branch: Optional[str] = None
    # Branches never attempted because an earlier branch failed and
    # halted the sequence.
    unattempted_branches: list[str] = field(default_factory=list)

    # --- Failure reason ---
    # Typed-or-string for compatibility with the ~60 legacy string literals
    # scattered across the orchestrator: in normal runtime usage the
    # orchestrator assigns the string form (or ``FailureReason.X.legacy_string``)
    # while construction sites in tests and external callers may pass the
    # raw :class:`FailureReason` enum directly.  ``to_legacy_dict`` and
    # ``failure_reason_enum`` accept either form.  Callers that want typed
    # access SHOULD use :attr:`failure_reason_enum`.
    failure_reason: Optional[Union[str, FailureReason]] = None
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
    # When True, the on-disk version was strictly higher than the
    # computed target (as opposed to merely equal).  This is a
    # stronger anomaly signal — see version_aggregator.py C1 docs.
    version_higher_than_target: bool = False

    # --- Logging ---
    log_file: Optional[Path] = None

    # --- Cleanup ---
    cleanup_report: Optional[CleanupReport] = None
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

    # --- Committed-issue ID reconciliation (git three-way-merge channel) ---
    # Issues renumbered because a clean git merge brought in a committed issue
    # file whose numeric ID already named a different issue on the current
    # branch. Each entry is an ``IssueMergeRecord(old_id, new_id, status_dir)``
    # — the same record type the runtime-sync channel emits — so both merge
    # channels report renumbers through one uniform shape.
    committed_issue_renumbers: list = field(default_factory=list)

    # ``#old`` references whose renumber target could not be proven because
    # SEVERAL merge-introduced issues shared the same old numeric ID. Such a
    # reference is deliberately left un-rewritten (rewriting to a guess would
    # silently corrupt it); each entry
    # ``{"file": <repo-relative path>, "old_id": "005",
    # "candidates": ["011", "012"]}`` surfaces the ambiguity to operators,
    # mirroring the note the reconciler appends inside the affected issue.
    ambiguous_issue_references: list = field(default_factory=list)

    # --- Rollback state ---
    # No live merge phase sets this: the spec-guardrails chain that used to roll
    # a merge back on violation is gone, and every other failure path aborts the
    # merge rather than resetting past it. The field and the CLI branch that
    # reads it survive so an archived report that recorded a failed rollback
    # still renders instead of losing its most severe signal.
    rollback_failed: bool = False

    # --- Bump inference diagnostics ---
    # Branches whose SemVer bump could not be inferred (transient git
    # timeout, parse error, etc.).  Populated by
    # ``_record_branch_bump`` so operators can see when a branch's
    # contribution to the aggregated bump was silently dropped.
    # Each entry is ``(branch, reason)`` where ``reason`` describes
    # the transient failure (e.g. ``"timeout"`` or ``"infer_error"``)
    # plus a short text — the typed contract is opaque enough that
    # callers don't make decisions on the string but rich enough for
    # log inspection.
    bump_inference_failures: list[tuple[str, str]] = field(
        default_factory=list
    )

    @property
    def failure_reason_enum(self) -> Optional[FailureReason]:
        """Typed :class:`FailureReason` for ``failure_reason``.

        Parses the string ``failure_reason`` field into the enum.  Returns
        ``None`` when no failure reason is set.  Unknown legacy strings map
        to :data:`FailureReason.UNEXPECTED` with the raw string preserved
        in ``failure_reason`` so no diagnostic information is lost.

        ``failure_reason`` may already be a :class:`FailureReason` enum
        (see the field's ``Union[str, FailureReason]`` type).  In that
        case the enum value is returned directly.
        """
        if self.failure_reason is None:
            return None
        if isinstance(self.failure_reason, FailureReason):
            return self.failure_reason
        from .failure_reason import from_legacy_string
        reason, _detail = from_legacy_string(self.failure_reason)
        return reason

    def add_outcome(self, outcome: MergeOutcome) -> None:
        """Record a branch outcome and update the summary buckets."""
        self.outcomes.append(outcome)
        if outcome.already_ancestor:
            self.already_ancestor_branches.append(outcome.branch)
        elif outcome.success:
            if outcome.warnings_repaired:
                self.merged_with_warnings.append(outcome.branch)
            else:
                self.newly_merged_branches.append(outcome.branch)
        elif (
            outcome.failure_reason is not None
            and outcome.failure_reason is not FailureReason.PENDING_HUMAN
        ):
            # Record the first failing branch so callers can see which
            # branch halted the sequence without re-parsing outcomes.
            if self.failed_branch is None:
                self.failed_branch = outcome.branch
        # Pending-human is not a failure in the traditional sense but
        # also not a success. It sits in its own state.

    @property
    def newly_merged(self) -> list[str]:
        """Alias for ``newly_merged_branches``.

        Preserves compatibility with code written during the brief
        window when the typed model used ``newly_merged``.
        """
        return self.newly_merged_branches

    @property
    def already_merged(self) -> list[str]:
        """Alias for ``already_ancestor_branches``."""
        return self.already_ancestor_branches

    @property
    def all_merged_branches(self) -> list[str]:
        """All branches that were either newly merged or already ancestors.

        This is the **backward-compatible** view for callers that
        expect a single ``merged_branches`` list.  New code should
        prefer the semantic buckets.
        """
        return (
            self.newly_merged_branches
            + self.already_ancestor_branches
            + self.merged_with_warnings
        )

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
        result: dict = {
            "success": self.success,
            "merged_branches": self.all_merged_branches,
            "newly_merged_branches": self.newly_merged_branches,
            "already_ancestor_branches": self.already_ancestor_branches,
            "merged_with_warnings": self.merged_with_warnings,
            "failed_branch": self.failed_branch,
            "failure_reason": (
                self.failure_reason.legacy_string
                if isinstance(self.failure_reason, FailureReason)
                else (self.failure_reason or "")
            ),
            "failure_detail": self.failure_detail,
            "pending_human": self.pending_human,
            "human_call_file": self.call_file_str,
            "log_file": str(self.log_file) if self.log_file else None,
            "pre_merge_version": self.pre_merge_version,
            "effective_pre_merge_version": self.effective_pre_merge_version,
            "final_version": self.final_version,
            "bump_type": self.bump_type,
            "version_aggregation_skipped": self.version_aggregation_skipped,
            "version_aggregation_error": self.version_aggregation_error,
            "version_higher_than_target": self.version_higher_than_target,
            "cleanup_skipped": self.cleanup_skipped,
            "runtime_sync_skipped_branches": self.runtime_sync_skipped_branches,
            "runtime_sync_skipped_files": self.runtime_sync_skipped_files,
            "runtime_sync_discarded": self.runtime_sync_discarded,
            "runtime_sync_collision_path": self.runtime_sync_collision_path,
            "runtime_sync_idempotent_bypasses": self.runtime_sync_idempotent_bypasses,
            # Serialized as plain dicts (not IssueMergeRecord instances) so the
            # legacy-dict consumers (JSON persistence, log inspection) need no
            # knowledge of the runtime_sync record type.
            "committed_issue_renumbers": [
                {
                    "old_id": r.old_id,
                    "new_id": r.new_id,
                    "status_dir": r.status_dir,
                }
                for r in self.committed_issue_renumbers
            ],
            "ambiguous_issue_references": [
                dict(entry) for entry in self.ambiguous_issue_references
            ],
            "rollback_failed": self.rollback_failed,
            "unattempted_branches": self.unattempted_branches,
            "outcomes": [
                {
                    "branch": o.branch,
                    "success": o.success,
                    "failure_reason": o.failure_reason.legacy_string if o.failure_reason else "",
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
