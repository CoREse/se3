"""MergeOrchestrator — Sequential merge of branches into current branch.

Orchestrates the merge flow: for each branch, call git merge, handle
clean merge / conflict / non-conflict-failure, run guardrails, and
aggregate results.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..version_bumper import BumpType
from ..worktree import _run_git, get_conflicting_files, get_current_branch
from .cleanup import CleanupManager, CleanupReport
from .conflict_context import build as build_conflict_context
from .conflict_resolver import ConflictResolver, LLMResolution, MergeStrategy
from .guardrail_repair import GuardrailRepairer
from .guardrails import (
    MergeGuardrailsCheck,
    _get_changed_spec_files,
    _read_file_from_ref,
)
from .human_call import HumanCallWriter
from .strategy import DecisionAction, StrategyDecider, StrategyDecision
from .version_aggregator import (
    aggregate_and_apply,
    infer_branch_bump,
    read_version_at_ref,
)

logger = logging.getLogger(__name__)


class GuardrailRollbackError(RuntimeError):
    """Raised when guardrails detected violations but rollback could not be performed.

    Carries the path to the human call file so the caller can still
    surface it in the report even though the rollback failed.
    """

    def __init__(self, message: str, call_file: Optional[Path] = None) -> None:
        super().__init__(message)
        self.call_file = call_file


class GuardrailNoRollbackError(RuntimeError):
    """Raised when guardrails detected violations but rollback was never attempted.

    This happens when the pre-merge SHA is unavailable, so there is no
    known state to roll back to.  The merge commit may still be on HEAD.
    Distinct from ``GuardrailRollbackError``: here no rollback command was
    issued at all, whereas ``GuardrailRollbackError`` means a rollback
    command was issued and failed.
    """

    def __init__(self, message: str, call_file: Optional[Path] = None) -> None:
        super().__init__(message)
        self.call_file = call_file


class GuardrailCallFileError(RuntimeError):
    """Raised when guardrails detected violations, rollback succeeded, but the
    human call file could not be written or printed.

    This is distinct from ``GuardrailRollbackError``: here the working tree is
    in a consistent (rolled-back) state, but the user has no call file to respond
    to.  The caller should report the true failure mode rather than claiming
    rollback failed.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class GuardrailRepairFailed(RuntimeError):
    """Raised when fast-mode guardrail repair fails.

    In ``fast`` strategy, post-merge guardrail violations are sent to the LLM
    for repair. If the LLM cannot fix them, or if the repair process itself
    fails, this exception is raised so the orchestrator can abort cleanly
    without writing a human call file.
    """

    def __init__(
        self,
        message: str,
        failure_reason: str = "guardrail_repair_failed",
        rollback_failed: bool = False,
    ) -> None:
        super().__init__(message)
        self.failure_reason = failure_reason
        self.rollback_failed = rollback_failed


@dataclass
class MergeReport:
    """Result of a merge orchestration run."""

    success: bool = False
    merged_branches: list[str] = field(default_factory=list)
    failed_branch: Optional[str] = None
    failure_reason: Optional[str] = None
    pending_human: bool = False
    human_call_file: Optional[Path] = None
    log_file: Optional[Path] = None
    pre_merge_version: Optional[str] = None
    final_version: Optional[str] = None
    bump_type: Optional[str] = None
    version_aggregation_skipped: bool = False
    version_aggregation_error: Optional[str] = None
    cleanup_report: Optional[CleanupReport] = None
    cleanup_skipped: bool = True
    rollback_failed: bool = False


class MergeOrchestrator:
    """Orchestrate sequential merging of branches.

    For each branch:
      1. ``git merge <branch> --no-edit``
      2. Clean merge  → guardrails check → commit recorded, continue
      3. Conflict     → build context → LLM resolve → strategy decide
         - ACCEPT  → write back → git add → git commit → guardrails → continue
         - HUMAN_CALL → write call file → pause (pending_human)
         - REJECT  → git merge --abort → stop
      4. Non-conflict failure → ``git merge --abort`` → stop
    """

    def __init__(
        self,
        project_root: Path,
        strategy: str = "default",
        delete_merged: bool = False,
    ) -> None:
        self.project_root = project_root
        if strategy not in ("default", "strict", "fast"):
            raise ValueError(
                f"Unknown merge strategy: {strategy!r}. "
                f"Must be one of: default, strict, fast"
            )
        self.strategy = MergeStrategy(strategy)
        self.delete_merged = delete_merged
        self.log_file: Optional[Path] = None
        self._log_lines: list[str] = []
        self._resolver = ConflictResolver(project_root)
        self._decider = StrategyDecider()
        self._human_writer = HumanCallWriter(project_root)
        self._guardrails = MergeGuardrailsCheck(project_root)
        self._repairer = GuardrailRepairer(project_root)

    def _log(self, message: str) -> None:
        """Append a line to the internal log buffer and the logger."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}"
        self._log_lines.append(line)
        logger.info(message)

    def _write_log(self) -> None:
        """Flush the log buffer to se3/logs/merge_<ts>.log."""
        logs_dir = self.project_root / "se3" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.log_file = logs_dir / f"merge_{ts}.log"
        self.log_file.write_text("\n".join(self._log_lines) + "\n", encoding="utf-8")

    def execute(self, branches: list[str]) -> MergeReport:
        """Execute sequential merge of all branches.

        Args:
            branches: Branch names to merge, in order.

        Returns:
            MergeReport summarizing the outcome.
        """
        report = MergeReport()
        current_branch = get_current_branch(self.project_root)
        self._current_branch = current_branch

        self._log("Merge orchestrator starting")
        self._log(f"Current branch: {current_branch}")
        self._log(f"Branches to merge ({len(branches)}): {', '.join(branches)}")
        self._log(f"Strategy: {self.strategy.value}")

        # Capture pre-merge state for SemVer aggregation
        try:
            pre_merge_sha_result = _run_git(
                self.project_root, "rev-parse", "HEAD",
                check=False, timeout=15,
            )
            pre_merge_sha = (
                pre_merge_sha_result.stdout.strip()
                if pre_merge_sha_result.returncode == 0
                else ""
            )
        except subprocess.TimeoutExpired:
            self._log("git rev-parse HEAD timed out — cannot capture pre-merge SHA")
            pre_merge_sha = ""
        pre_merge_version = (
            read_version_at_ref(self.project_root, pre_merge_sha)
            if pre_merge_sha
            else None
        )
        if pre_merge_version:
            report.pre_merge_version = pre_merge_version
            self._log(f"Pre-merge version: {pre_merge_version}")
        else:
            self._log("Pre-merge version: <unavailable>")

        branch_bumps: list[BumpType] = []

        for branch in branches:
            self._log(f"--- Merging branch: {branch} ---")

            result = self._merge_single_branch(branch, report)

            if result == "merged" or result == "already_merged":
                self._log(f"Branch '{branch}' merged successfully")
                report.merged_branches.append(branch)
                if (
                    result == "merged"
                    and pre_merge_sha
                    and pre_merge_version
                ):
                    try:
                        bump = infer_branch_bump(
                            self.project_root, branch, pre_merge_sha,
                        )
                        if bump is not None:
                            branch_bumps.append(bump)
                            self._log(f"Inferred bump for '{branch}': {bump.value}")
                        else:
                            self._log(
                                f"No version metadata for '{branch}' — skip bump"
                            )
                    except Exception as exc:
                        self._log(f"Failed to infer bump for '{branch}': {exc}")
            elif result == "conflict":
                self._log(f"Branch '{branch}' has conflicts — aborting")
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — re-run after resolving"
                    )
                report.success = False
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = "merge_conflict"
                report.version_aggregation_skipped = True
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result == "pending_human":
                self._log(f"Branch '{branch}' paused for human review")
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — re-run after resolving"
                    )
                report.success = False
                report.pending_human = True
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = "pending_human"
                report.version_aggregation_skipped = True
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result == "guardrail_violation":
                self._log(f"Branch '{branch}' rolled back due to guardrail violation")
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — re-run after resolving"
                    )
                report.success = False
                report.pending_human = True
                report.failed_branch = branch
                report.failure_reason = "guardrail_violation"
                report.version_aggregation_skipped = True
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result == "guardrail_violation_call_failed":
                self._log(
                    f"Branch '{branch}' guardrail violation detected. Rollback "
                    f"succeeded, but the human call file could not be written. "
                    f"Manual intervention required."
                )
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — re-run after resolving"
                    )
                report.success = False
                report.failed_branch = branch
                report.failure_reason = "guardrail_violation_call_failed"
                report.version_aggregation_skipped = True
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result == "guardrail_violation_no_rollback":
                self._log(
                    f"Branch '{branch}' guardrail violation detected. "
                    f"Rollback was not attempted because pre_merge_sha was missing. "
                    f"The merge commit may still be in HEAD. See the human call file."
                )
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — re-run after resolving"
                    )
                report.success = False
                report.pending_human = True
                report.failed_branch = branch
                report.failure_reason = "guardrail_violation_no_rollback"
                report.rollback_failed = False
                report.version_aggregation_skipped = True
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result == "rollback_failed":
                self._log(
                    f"Branch '{branch}' guardrail violation detected but ROLLBACK FAILED. "
                    f"Working tree is in an inconsistent state. Manual intervention required."
                )
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — re-run after resolving"
                    )
                report.success = False
                report.rollback_failed = True
                report.failed_branch = branch
                report.failure_reason = "rollback_failed"
                report.version_aggregation_skipped = True
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result == "merge_abort_failed":
                self._log(
                    f"Branch '{branch}' aborted but git merge --abort FAILED. "
                    f"Working tree may still be mid-merge. Manual intervention required."
                )
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — re-run after resolving"
                    )
                report.success = False
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = "merge_abort_failed"
                # If a human call file was written before the abort failed,
                # surface it to the user so they know there is a call file
                # to respond to (even though the working tree may be inconsistent).
                if report.human_call_file:
                    report.pending_human = True
                report.version_aggregation_skipped = True
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result == "non_conflict_failure":
                self._log(f"Branch '{branch}' merge failed (non-conflict) — aborting")
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — re-run after resolving"
                    )
                report.success = False
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = "merge_failed"
                report.version_aggregation_skipped = True
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result == "resolution_commit_timeout":
                self._log(
                    f"Branch '{branch}' conflict resolution succeeded but "
                    f"git commit timed out — aborting"
                )
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — re-run after resolving"
                    )
                report.success = False
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = "resolution_commit_timeout"
                report.version_aggregation_skipped = True
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result == "incomplete_resolution_call_failed":
                self._log(
                    f"Branch '{branch}' had incomplete LLM resolution and the "
                    f"human call file could not be written. "
                    f"Manual intervention required."
                )
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — re-run after resolving"
                    )
                report.success = False
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = "incomplete_resolution_call_failed"
                report.version_aggregation_skipped = True
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result == "human_call_write_failed":
                self._log(
                    f"Branch '{branch}' conflict resolution required human review, but "
                    f"the human call file could not be written. "
                    f"Manual intervention required."
                )
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — re-run after resolving"
                    )
                report.success = False
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = "human_call_write_failed"
                report.version_aggregation_skipped = True
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result == "fast_abort":
                self._log(
                    f"Branch '{branch}' aborted in fast mode — no human call created"
                )
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — re-run after resolving"
                    )
                report.success = False
                report.failed_branch = branch
                # Use the failure_reason already set by the lower layer if present
                if not report.failure_reason:
                    report.failure_reason = "fast_abort"
                report.pending_human = False
                report.version_aggregation_skipped = True
                # If rollback_failed was set (e.g. by GuardrailRepairFailed),
                # log a CRITICAL warning so the log file captures the severity
                # even though merge_cmd.py will surface it in the CLI via
                # its report.rollback_failed branch.
                if report.rollback_failed:
                    self._log(
                        f"CRITICAL: Branch '{branch}' guardrail violation detected "
                        f"but ROLLBACK FAILED. Working tree is in an inconsistent state. "
                        f"Manual intervention required."
                    )
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result == "binary_file_conflict":
                self._log(
                    f"Branch '{branch}' aborted — binary file conflict requires human review"
                )
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — re-run after resolving"
                    )
                report.success = False
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = "binary_file_conflict"
                # If a human call was written, treat as pending human review
                if report.human_call_file:
                    report.pending_human = True
                report.version_aggregation_skipped = True
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result == "resolution_validation_failed":
                self._log(
                    f"Branch '{branch}' aborted — resolved content failed validation"
                )
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — re-run after resolving"
                    )
                report.success = False
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = "resolution_validation_failed"
                # If a human call was written, treat as pending human review
                if report.human_call_file:
                    report.pending_human = True
                report.version_aggregation_skipped = True
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result == "resolution_write_failed":
                self._log(
                    f"Branch '{branch}' aborted — failed to write or stage resolved files"
                )
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — re-run after resolving"
                    )
                report.success = False
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = "resolution_write_failed"
                report.version_aggregation_skipped = True
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result == "resolution_commit_failed":
                self._log(
                    f"Branch '{branch}' aborted — merge commit failed after resolution"
                )
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — re-run after resolving"
                    )
                report.success = False
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = "resolution_commit_failed"
                report.version_aggregation_skipped = True
                self._write_log()
                report.log_file = self.log_file
                return report
            else:
                self._log(f"Branch '{branch}' merge returned unexpected result: {result}")
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — re-run after resolving"
                    )
                report.success = False
                report.failed_branch = branch
                report.failure_reason = "unexpected"
                report.version_aggregation_skipped = True
                self._write_log()
                report.log_file = self.log_file
                return report

        # All branches merged successfully
        report.success = True
        self._log(f"All {len(branches)} branch(es) merged successfully")
        self._log(f"Merged: {', '.join(report.merged_branches)}")

        # SemVer aggregation: apply max bump to pyproject.toml and amend
        if pre_merge_version and branch_bumps:
            self._log("Aggregating SemVer bumps from merged branches")
            try:
                agg = aggregate_and_apply(
                    self.project_root, branch_bumps, pre_merge_version,
                )
                if agg.success:
                    report.final_version = agg.new_version
                    if agg.bump_type is not None:
                        report.bump_type = agg.bump_type.value
                    self._log(
                        f"Version aggregated: {pre_merge_version} → {agg.new_version} "
                        f"({agg.bump_type.value if agg.bump_type else 'unknown'})"
                    )
                else:
                    report.version_aggregation_error = agg.error
                    report.version_aggregation_skipped = True
                    self._log(f"Version aggregation failed: {agg.error}")
            except Exception as exc:
                report.version_aggregation_error = str(exc)
                report.version_aggregation_skipped = True
                self._log(f"Version aggregation raised: {exc}")
        else:
            report.version_aggregation_skipped = True
            if not pre_merge_version:
                self._log("Skipping version aggregation: no pre-merge version available")
            elif not branch_bumps:
                self._log("Skipping version aggregation: no branches contributed bumps")

        # --delete-merged: clean up branches and worktrees
        if self.delete_merged and report.success:
            report.cleanup_skipped = False
            self._log("Running cleanup for --delete-merged")
            try:
                cleanup = CleanupManager(self.project_root)
                cr = cleanup.delete_merged_branches(report.merged_branches)
                report.cleanup_report = cr
                if cr.deleted:
                    self._log(f"Deleted branches: {', '.join(cr.deleted)}")
                if cr.skipped_dirty:
                    for b, reason in cr.skipped_dirty:
                        self._log(f"Skipped dirty branch '{b}': {reason}")
                if cr.skipped_worktree_remove_failed:
                    for b, reason in cr.skipped_worktree_remove_failed:
                        self._log(
                            f"Skipped branch '{b}' (worktree remove failed): {reason}",
                        )
                if cr.skipped_protected:
                    for b in cr.skipped_protected:
                        self._log(f"Skipped protected branch '{b}'")
                if cr.skipped_not_merged:
                    for b, reason in cr.skipped_not_merged:
                        self._log(f"Skipped not-fully-merged branch '{b}': {reason}")
            except Exception as exc:
                self._log(f"Cleanup failed: {exc}")
                # Ensure a report exists even when cleanup blows up part-way
                if report.cleanup_report is None:
                    report.cleanup_report = CleanupReport(
                        skipped_not_merged=[(
                            "<cleanup-aborted>",
                            f"Cleanup raised {type(exc).__name__}: {exc}",
                        )],
                    )
        else:
            if not self.delete_merged:
                self._log("Cleanup skipped: --delete-merged not set")
            elif not report.success:
                self._log("Cleanup skipped: merge did not fully succeed")

        self._write_log()
        report.log_file = self.log_file
        return report

    def _merge_single_branch(self, branch: str, report: MergeReport) -> str:
        """Merge a single branch and classify the outcome.

        Returns:
            One of: "merged", "conflict", "pending_human",
            "guardrail_violation", "non_conflict_failure".
        """
        # Remember pre-merge HEAD for guardrails check and rollback
        try:
            pre_merge_head = _run_git(
                self.project_root, "rev-parse", "HEAD",
                check=False, timeout=15,
            )
            pre_merge_sha = pre_merge_head.stdout.strip() if pre_merge_head.returncode == 0 else ""
        except subprocess.TimeoutExpired:
            self._log("git rev-parse HEAD timed out — cannot capture pre-merge SHA for rollback")
            pre_merge_sha = ""

        # Run git merge
        try:
            result = _run_git(
                self.project_root,
                "merge",
                branch,
                "--no-ff",
                "--no-edit",
                "-m", f"Merge branch '{branch}'",
                check=False,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            self._log(f"git merge timed out for branch '{branch}'")
            abort_ok = self._abort_merge()
            if self.strategy == MergeStrategy.FAST:
                if not abort_ok:
                    report.failure_reason = "merge_abort_failed"
                else:
                    report.failure_reason = "merge_timed_out"
                return "fast_abort"
            if not abort_ok:
                report.failure_reason = "merge_abort_failed"
            else:
                report.failure_reason = "merge_timed_out"
            return "non_conflict_failure"

        if result.returncode == 0:
            # Clean merge — run guardrails on spec files
            rev_parse_result = None
            try:
                rev_parse_result = _run_git(
                    self.project_root, "rev-parse", "HEAD",
                    check=False, timeout=15,
                )
                post_merge_sha = rev_parse_result.stdout.strip()
            except subprocess.TimeoutExpired:
                self._log(
                    f"git rev-parse HEAD timed out after clean merge of '{branch}'. "
                    "Cannot verify merge commit SHA; treating as failure."
                )
                post_merge_sha = ""
            if not post_merge_sha:
                rc_str = (
                    f"rc={rev_parse_result.returncode}"
                    if rev_parse_result is not None
                    else "timeout"
                )
                self._log(
                    f"WARNING: git rev-parse HEAD failed ({rc_str}) "
                    f"after clean merge of '{branch}'. This indicates possible git state "
                    f"corruption; guardrails will fail closed."
                )

            # Detect no-op (already-up-to-date) merge: HEAD did not change
            if post_merge_sha and post_merge_sha == pre_merge_sha:
                self._log(
                    f"Branch '{branch}' is already an ancestor — "
                    f"no-op merge, skipping bump inference"
                )
                return "already_merged"

            try:
                guardrails_result = self._run_guardrails(
                    pre_merge_sha, post_merge_sha, branch, strategy=self.strategy,
                )
            except GuardrailRepairFailed as exc:
                if exc.failure_reason == "guardrail_check_failed":
                    self._log(
                        f"Guardrails check itself crashed for '{branch}' in fast mode: {exc}"
                    )
                else:
                    self._log(
                        f"Guardrail repair failed for '{branch}' in fast mode: {exc}"
                    )
                report.rollback_failed = getattr(exc, "rollback_failed", False)
                report.failure_reason = exc.failure_reason
                return "fast_abort"
            except GuardrailCallFileError as exc:
                self._log(
                    f"Guardrail violation detected, rollback succeeded, but "
                    f"call file could not be written: {exc}"
                )
                report.rollback_failed = False
                report.failure_reason = "guardrail_violation_call_failed"
                return "guardrail_violation_call_failed"
            except GuardrailNoRollbackError as exc:
                self._log(
                    f"Guardrails check for '{branch}' failed: {exc}. "
                    f"Rollback was not attempted because pre_merge_sha was missing. "
                    f"The merge commit may still be in HEAD."
                )
                report.rollback_failed = False
                if exc.call_file is not None:
                    report.human_call_file = exc.call_file
                return "guardrail_violation_no_rollback"
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                self._log(f"Rollback failed after guardrail violation: {exc}")
                report.rollback_failed = True
                if hasattr(exc, "call_file") and exc.call_file is not None:
                    report.human_call_file = exc.call_file
                return "rollback_failed"
            if guardrails_result is not None:
                report.human_call_file = guardrails_result
                return "guardrail_violation"
            # If fast-mode guardrail repair amended the commit, HEAD changed.
            # Refresh post_merge_sha so any downstream logging stays accurate.
            try:
                post_merge_sha = _run_git(
                    self.project_root, "rev-parse", "HEAD",
                    check=False, timeout=15,
                ).stdout.strip()
            except subprocess.TimeoutExpired:
                self._log(
                    f"git rev-parse HEAD timed out after guardrails check for '{branch}'. "
                    "Downstream SHA may be stale."
                )
                # Keep the old post_merge_sha (or empty if already unset)
            return "merged"

        # Merge failed — determine if it's a conflict or something else
        is_conflict = (
            "CONFLICT" in result.stdout
            or "CONFLICT" in result.stderr
            or "conflict" in result.stderr.lower()
        )

        # Also check for actual conflicting files as a secondary signal
        conflict_files = get_conflicting_files(self.project_root)
        if conflict_files:
            is_conflict = True

        if is_conflict:
            return self._handle_conflict(branch, pre_merge_sha, report)

        # Non-conflict failure — log stderr, abort and report
        stderr_msg = result.stderr.strip()
        if stderr_msg:
            self._log(f"git merge stderr: {stderr_msg}")
        if self.strategy == MergeStrategy.FAST:
            if not self._abort_merge():
                self._log(
                    "WARNING: git merge --abort failed — working tree may still be mid-merge"
                )
                report.failure_reason = "merge_abort_failed"
            else:
                # Use 'fast_failure:' (not 'fast_abort:') for non-conflict
                # git failures so the CLI can distinguish them from conflict-
                # resolution aborts and produce an accurate message.
                report.failure_reason = (
                    f"fast_failure: {stderr_msg}" if stderr_msg else "fast_failure"
                )
            return "fast_abort"
        if not self._abort_merge():
            self._log(
                "WARNING: git merge --abort failed — working tree may still be mid-merge"
            )
            report.failure_reason = "merge_abort_failed"
        else:
            report.failure_reason = f"merge_failed: {stderr_msg}" if stderr_msg else "merge_failed"
        return "non_conflict_failure"

    def _handle_conflict(
        self,
        branch: str,
        pre_merge_sha: str,
        report: MergeReport,
    ) -> str:
        """Handle a merge conflict with LLM resolution.

        Returns:
            "merged" (if resolved and committed), "pending_human",
            "guardrail_violation", "fast_abort", or "conflict" (if rejected/aborted).
        """
        # Build conflict context (must be called while mid-merge)
        try:
            ours_branch = getattr(self, "_current_branch", "HEAD")
            context = build_conflict_context(self.project_root, ours_branch, branch)
        except Exception as exc:
            self._log(f"Failed to build conflict context: {exc}")
            abort_ok = self._abort_merge()
            if self.strategy == MergeStrategy.FAST:
                if not abort_ok:
                    report.failure_reason = "merge_abort_failed"
                else:
                    report.failure_reason = "conflict_context_failed"
                return "fast_abort"
            if self.strategy == MergeStrategy.STRICT and abort_ok:
                # Strict contract: any conflict escalates to human call.
                # Write a degraded call file since we cannot build full context.
                try:
                    call_file = self._human_writer.write_guardrail_call(
                        branch=branch,
                        violations=[
                            {
                                "file_path": "N/A",
                                "violation_type": "CONFLICT_CONTEXT_BUILD_FAILURE",
                                "message": (
                                    f"Conflict context could not be built: {exc}. "
                                    f"The merge has been aborted. "
                                    f"Please inspect the branch and resolve manually."
                                ),
                            }
                        ],
                        pre_merge_sha=pre_merge_sha,
                    )
                    report.human_call_file = call_file
                    report.failure_reason = "conflict_context_failed"
                    try:
                        self._human_writer.print_instructions(call_file)
                    except Exception as print_exc:
                        self._log(
                            f"WARNING: Failed to print instructions "
                            f"(call file was written): {print_exc}"
                        )
                    return "pending_human"
                except Exception as write_exc:
                    self._log(
                        f"CRITICAL: Failed to write degraded human call file for "
                        f"strict mode: {write_exc}. The merge is being aborted."
                    )
                    report.failure_reason = (
                        "conflict_context_failed_call_file_write_failed"
                    )
            if not abort_ok:
                report.failure_reason = "merge_abort_failed"
            elif not report.failure_reason:
                report.failure_reason = "conflict_context_failed"
            return "conflict"

        # --- STRICT: short-circuit to human call, skip LLM ---
        if self.strategy == MergeStrategy.STRICT:
            self._log(
                f"Strict strategy: skipping LLM resolution for '{branch}', "
                f"routing directly to human call"
            )
            # Build a placeholder resolution from working tree content
            from .conflict_resolver import Confidence, FileResolution, HunkResolution, LLMResolution
            placeholder_files: list[FileResolution] = []
            for cf in context.files:
                # In strict mode the LLM is skipped, so the "resolved_content"
                # is a placeholder that prevents a downstream merge-respond
                # consumer from accidentally writing conflict markers back.
                if cf.is_binary:
                    strict_resolved = (
                        "[__SE3_STRICT_PLACEHOLDER__: binary file — LLM resolution was skipped. "
                        "Please resolve conflicts manually. DO NOT accept this as final content.]"
                    )
                else:
                    strict_resolved = (
                        "[__SE3_STRICT_PLACEHOLDER__: LLM resolution was skipped. "
                        "Please resolve conflicts manually. DO NOT accept this as final content.]"
                    )
                placeholder_files.append(
                    FileResolution(
                        path=cf.path,
                        resolved_content=strict_resolved,
                        hunks=[
                            HunkResolution(
                                start_line=h.start_line,
                                end_line=h.end_line,
                                confidence=Confidence.LOW,
                                reasoning="Strict strategy: LLM resolution skipped",
                            )
                            for h in cf.hunks
                        ],
                        overall_confidence=Confidence.LOW,
                        flags={},
                        is_spec=cf.is_spec,
                    )
                )
            placeholder_resolution = LLMResolution(
                files=placeholder_files,
                overall_confidence=Confidence.LOW,
                flags={"llm_invoked": False},
            )
            strict_decision = StrategyDecision(
                action=DecisionAction.HUMAN_CALL,
                reason="Strict strategy: conflict detected, LLM resolution skipped — human review required",
            )
            try:
                call_file = self._human_writer.write_call(
                    context, placeholder_resolution, strict_decision,
                    strategy="strict",
                )
            except Exception as exc:
                self._log(
                    f"CRITICAL: Failed to write human call file for strict mode: {exc}. "
                    f"The merge is being aborted because the user has no call file to respond to."
                )
                if not self._abort_merge():
                    self._log(
                        "WARNING: git merge --abort failed — working tree may still be mid-merge"
                    )
                    report.failure_reason = "merge_abort_failed"
                return "human_call_write_failed"
            report.human_call_file = call_file
            try:
                self._human_writer.print_instructions(call_file)
            except Exception as exc:
                self._log(
                    f"WARNING: Failed to print instructions (call file was written "
                    f"successfully): {exc}"
                )
            return "pending_human"

        # --- DEFAULT / FAST: call LLM resolver ---
        self._log(f"Conflict detected with branch '{branch}', invoking LLM resolution")

        try:
            resolution = self._resolver.resolve(context, strategy=self.strategy)
        except Exception as exc:
            self._log(f"LLM resolution failed: {exc}")
            if self.strategy == MergeStrategy.FAST:
                if not self._abort_merge():
                    report.failure_reason = "merge_abort_failed"
                else:
                    report.failure_reason = "llm_resolution_failed"
                return "fast_abort"
            # DEFAULT strategy: escalate to human call (do NOT abort yet)
            llm_fail_decision = StrategyDecision(
                action=DecisionAction.HUMAN_CALL,
                reason=f"LLM resolution system failure: {exc}",
            )
            from .conflict_resolver import Confidence, LLMResolution
            placeholder_resolution = LLMResolution(
                files=[],
                overall_confidence=Confidence.LOW,
                flags={},
            )
            try:
                call_file = self._human_writer.write_call(
                    context, placeholder_resolution, llm_fail_decision,
                )
            except Exception as write_exc:
                self._log(
                    f"CRITICAL: Failed to write human call file for LLM failure: "
                    f"{write_exc}. The merge is being aborted because the user has "
                    f"no call file to respond to."
                )
                if not self._abort_merge():
                    self._log(
                        "WARNING: git merge --abort failed — working tree may still be mid-merge"
                    )
                    report.failure_reason = "merge_abort_failed"
                return "human_call_write_failed"
            report.human_call_file = call_file
            try:
                self._human_writer.print_instructions(call_file)
            except Exception as print_exc:
                self._log(
                    f"WARNING: Failed to print instructions (call file was written "
                    f"successfully): {print_exc}"
                )
            # Leave working tree with conflict markers for human — do NOT abort
            return "pending_human"

        # Strategy decision
        decision = self._decider.decide(
            resolution,
            has_spec_files=context.has_spec_files,
            strategy=self.strategy,
        )

        self._log(f"Strategy decision: {decision.action.value} — {decision.reason}")

        if decision.action == DecisionAction.ACCEPT:
            # Pre-check: ensure resolution covers exactly the conflict files
            context_paths = {cf.path for cf in context.files}
            resolution_paths = {fr.path for fr in resolution.files}
            missing = context_paths - resolution_paths
            extras = resolution_paths - context_paths
            if missing or extras:
                if missing and extras:
                    reason_detail = (
                        f"missing {len(missing)} file(s): {', '.join(sorted(missing))}; "
                        f"extra {len(extras)} file(s): {', '.join(sorted(extras))}"
                    )
                elif missing:
                    reason_detail = (
                        f"missing {len(missing)} file(s): {', '.join(sorted(missing))}"
                    )
                else:
                    reason_detail = (
                        f"extra {len(extras)} file(s): {', '.join(sorted(extras))}"
                    )
                self._log(
                    f"LLM resolution incomplete: {reason_detail}"
                )
                # --- FAST: incomplete resolution → abort ---
                if self.strategy == MergeStrategy.FAST:
                    self._log(
                        f"Fast strategy: incomplete resolution — aborting merge"
                    )
                    if not self._abort_merge():
                        report.failure_reason = "merge_abort_failed"
                    else:
                        report.failure_reason = "incomplete_resolution"
                    return "fast_abort"
                incomplete_decision = StrategyDecision(
                    action=DecisionAction.HUMAN_CALL,
                    reason=f"LLM resolution incomplete — {reason_detail}",
                )
                incomplete_options = {
                    "abort": "Abort merge — run `git merge --abort` and stop",
                    "manual": "Resolve manually — edit files, then run `git add . && git commit`",
                }
                # Compute the call-file name once so instructions match the on-disk filename.
                ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                safe_branch = context.theirs_branch.replace("/", "-")
                call_file_name = f"merge_{ts}_{safe_branch}.json"
                try:
                    call_file = self._human_writer.write_call(
                        context,
                        resolution,
                        incomplete_decision,
                        options=incomplete_options,
                        instructions_override=(
                            f"Merge conflict in {context.theirs_branch} → {context.ours_branch}. "
                            f"WARNING: The LLM resolution is INCOMPLETE — {reason_detail}. "
                            f"'accept' is NOT available because unresolved files would remain. "
                            f"Choose 'manual' to resolve all files yourself, or 'abort' to cancel. "
                            f"To respond, create a file named '{call_file_name}.response' "
                            f"in the same directory with JSON: "
                            f"{{\"choice\": \"abort|manual\", \"feedback\": \"optional notes\"}}."
                        ),
                        call_file_name=call_file_name,
                    )
                except Exception as exc:
                    self._log(
                        f"CRITICAL: Failed to write human call file for incomplete "
                        f"resolution: {exc}. The merge is being aborted because the user "
                        f"has no call file to respond to."
                    )
                    if not self._abort_merge():
                        self._log(
                            "WARNING: git merge --abort failed — working tree may still be mid-merge"
                        )
                        report.failure_reason = "merge_abort_failed"
                    return "incomplete_resolution_call_failed"
                report.human_call_file = call_file
                try:
                    self._human_writer.print_instructions(call_file)
                except Exception as exc:
                    self._log(
                        f"WARNING: Failed to print instructions for incomplete "
                        f"resolution (call file was written successfully): {exc}"
                    )
                return "pending_human"
            return self._apply_resolution(branch, resolution, pre_merge_sha, context, report)

        if decision.action == DecisionAction.HUMAN_CALL:
            try:
                call_file = self._human_writer.write_call(context, resolution, decision)
            except Exception as exc:
                self._log(
                    f"CRITICAL: Failed to write human call file: {exc}. The merge is "
                    f"being aborted because the user has no call file to respond to."
                )
                if not self._abort_merge():
                    self._log(
                        "WARNING: git merge --abort failed — working tree may still be mid-merge"
                    )
                    report.failure_reason = "merge_abort_failed"
                return "human_call_write_failed"
            report.human_call_file = call_file
            try:
                self._human_writer.print_instructions(call_file)
            except Exception as exc:
                self._log(
                    f"WARNING: Failed to print instructions (call file was written "
                    f"successfully): {exc}"
                )
            # Do NOT abort — leave working tree with conflict markers for human
            return "pending_human"

        # REJECT
        abort_ok = self._abort_merge()
        if self.strategy == MergeStrategy.FAST:
            if not abort_ok:
                report.failure_reason = "merge_abort_failed"
            else:
                report.failure_reason = "resolution_rejected"
            return "fast_abort"
        return "conflict"

    def _apply_resolution(
        self,
        branch: str,
        resolution: LLMResolution,
        pre_merge_sha: str,
        context: "ConflictContext",
        report: MergeReport,
    ) -> str:
        """Write resolved content back, stage, commit, and run guardrails.

        Returns:
            "merged" on success, "guardrail_violation" if guardrails fail
            (after rollback), "resolution_validation_failed" if resolved
            content fails validation (after abort), "resolution_write_failed"
            if writing or staging resolved files fails (after abort),
            "resolution_commit_failed" if the merge commit fails after
            resolution (after abort), "rollback_failed" if guardrails detected
            violations but rollback could not be completed,
            "resolution_commit_timeout" if the post-resolution ``git commit``
            timed out (after abort).
        """
        # Build the set of valid paths and path→ConflictFile mapping
        valid_paths = {cf.path for cf in context.files}
        file_by_path = {cf.path: cf for cf in context.files}

        # --- First pass: validate all paths before mutating working tree ---
        add_failures = False
        for file_res in resolution.files:
            if not file_res.path:
                self._log("Rejected empty file path in resolution")
                add_failures = True
                continue

            # (a) Must be in git's index of conflicting files
            if file_res.path not in valid_paths:
                self._log(
                    f"Rejected file path not in conflict set: {file_res.path}"
                )
                add_failures = True
                continue
            # (b) Must not be absolute
            if Path(file_res.path).is_absolute():
                self._log(
                    f"Rejected absolute file path: {file_res.path}"
                )
                add_failures = True
                continue
            # (c) Must resolve inside project_root
            full_path = (self.project_root / file_res.path).resolve()
            try:
                full_path.relative_to(self.project_root.resolve())
            except ValueError:
                self._log(
                    f"Rejected file path outside project root: {file_res.path}"
                )
                add_failures = True
                continue

            # (d) Defense-in-depth: reject binary files (cannot auto-resolve)
            cf = file_by_path.get(file_res.path)
            if cf is not None and cf.is_binary:
                self._log(
                    f"Binary file conflict requires human review: {file_res.path}"
                )
                add_failures = True
                # Distinguish binary rejection from generic validation failure
                # so the CLI can surface a targeted message.
                self._binary_file_rejected = True  # type: ignore[attr-defined]
                continue

            # (e) Empty resolved content: verify working-tree file is safe to delete
            if not file_res.resolved_content:
                if full_path.exists():
                    try:
                        content = full_path.read_text(encoding="utf-8")
                        if "<<<<<<<" in content or ">>>>>>>" in content:
                            self._log(
                                f"Unresolved conflict markers in {file_res.path} — "
                                f"LLM returned empty content for a file that still has markers"
                            )
                            add_failures = True
                            continue
                    except UnicodeDecodeError:
                        self._log(
                            f"Binary file (undecodable) requires human review: {file_res.path}"
                        )
                        add_failures = True
                        continue
                    except Exception:
                        self._log(
                            f"Could not read {file_res.path} to verify deletion safety — "
                            f"skipping"
                        )
                        add_failures = True
                        continue
                # Confidence gate for delete-modify conflicts: if the file
                # has non-empty content in either ours or theirs, require
                # ALL hunks to have HIGH confidence before accepting deletion.
                # Run this regardless of whether full_path.exists() — stage
                # entries may still hold meaningful content even when the
                # working-tree copy is absent (e.g. rename/delete conflict).
                cf = file_by_path.get(file_res.path)
                if cf is not None and (
                    (cf.ours_exists and cf.ours_content.strip())
                    or (cf.theirs_exists and cf.theirs_content.strip())
                ):
                    from .conflict_resolver import Confidence
                    if file_res.hunks:
                        hunks_not_high = any(
                            h.confidence != Confidence.HIGH
                            for h in file_res.hunks
                        )
                    else:
                        # No hunks reported — fall back to overall confidence
                        hunks_not_high = (
                            file_res.overall_confidence != Confidence.HIGH
                        )
                    if hunks_not_high:
                        self._log(
                            f"Deletion of {file_res.path} rejected: file has "
                            f"content in ours/theirs but not all hunks have HIGH "
                            f"confidence"
                        )
                        add_failures = True
                        continue
                continue

            # (f) Reject resolved content that still contains conflict markers
            if "<<<<<<<" in file_res.resolved_content or ">>>>>>>" in file_res.resolved_content:
                self._log(
                    f"Unresolved conflict markers in resolved content for {file_res.path}"
                )
                add_failures = True
                continue

        if add_failures:
            # For non-fast strategies, write human call before aborting
            if self.strategy != MergeStrategy.FAST:
                from .strategy import DecisionAction, StrategyDecision
                if getattr(self, "_binary_file_rejected", False):
                    validation_reason = "Binary file conflict requires human review"
                else:
                    validation_reason = "Resolved content failed validation"
                validation_decision = StrategyDecision(
                    action=DecisionAction.HUMAN_CALL,
                    reason=validation_reason,
                )
                try:
                    call_file = self._human_writer.write_call(
                        context, resolution, validation_decision,
                    )
                    report.human_call_file = call_file
                except Exception as exc:
                    self._log(
                        f"CRITICAL: Failed to write human call file for validation "
                        f"failure: {exc}. The merge is being aborted without a call file."
                    )
            self._log("Aborting merge due to validation failures")
            abort_ok = self._abort_merge()
            # Capture and clean up the binary-file flag before any early
            # returns so a future retry on the same instance does not see
            # a stale value.
            was_binary = getattr(self, "_binary_file_rejected", False)
            if hasattr(self, "_binary_file_rejected"):
                delattr(self, "_binary_file_rejected")
            if not abort_ok:
                report.failure_reason = "merge_abort_failed"
                if self.strategy == MergeStrategy.FAST:
                    return "fast_abort"
                return "merge_abort_failed"
            # Use a dedicated reason when a binary file was in the conflict set
            # so the CLI can surface a targeted message.
            if was_binary:
                if self.strategy == MergeStrategy.FAST:
                    report.failure_reason = "binary_file_conflict_fast_abort"
                else:
                    report.failure_reason = "binary_file_conflict"
            else:
                report.failure_reason = "resolution_validation_failed"
            if self.strategy == MergeStrategy.FAST:
                return "fast_abort"
            # For non-fast, use the same reason we stored in report
            return report.failure_reason or "resolution_validation_failed"

        # --- Second pass: write and stage (all paths pre-validated) ---
        try:
            for file_res in resolution.files:
                full_path = (self.project_root / file_res.path).resolve()

                # Empty resolved content: deletion already validated in first pass
                if not file_res.resolved_content:
                    if full_path.exists():
                        # Deletion: use git rm -f to handle unmerged paths
                        rm_result = _run_git(
                            self.project_root, "rm", "-f", file_res.path,
                            check=False, timeout=15,
                        )
                    else:
                        # File absent from working tree (e.g. rename conflict)
                        # but may still have unmerged index entries — stage removal.
                        rm_result = _run_git(
                            self.project_root, "rm", "-f", "--ignore-unmatch", file_res.path,
                            check=False, timeout=15,
                        )
                    if rm_result.returncode != 0:
                        self._log(
                            f"Failed to rm {file_res.path}: {rm_result.stderr.strip()}"
                        )
                        add_failures = True
                        break
                    continue

                # Non-empty resolved content: write and stage
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(file_res.resolved_content, encoding="utf-8")

                # Stage the file
                add_result = _run_git(
                    self.project_root, "add", file_res.path,
                    check=False, timeout=15,
                )
                if add_result.returncode != 0:
                    self._log(
                        f"Failed to stage {file_res.path}: {add_result.stderr.strip()}"
                    )
                    add_failures = True
                    break

            # Abort if any file failed during second pass
            if add_failures:
                self._log("Aborting merge due to write/stage failures")
                abort_ok = self._abort_merge()
                if not abort_ok:
                    report.failure_reason = "merge_abort_failed"
                    if self.strategy == MergeStrategy.FAST:
                        return "fast_abort"
                    return "merge_abort_failed"
                report.failure_reason = "resolution_write_failed"
                if self.strategy == MergeStrategy.FAST:
                    return "fast_abort"
                return "resolution_write_failed"

        except Exception as exc:
            self._log(f"Exception during resolution application: {exc}")
            abort_ok = self._abort_merge()
            if not abort_ok:
                report.failure_reason = "merge_abort_failed"
                if self.strategy == MergeStrategy.FAST:
                    return "fast_abort"
                return "merge_abort_failed"
            report.failure_reason = "resolution_write_failed"
            if self.strategy == MergeStrategy.FAST:
                return "fast_abort"
            return "resolution_write_failed"

        # Commit the merge
        try:
            commit_result = _run_git(
                self.project_root, "commit", "--no-edit",
                "-m", f"Merge branch '{branch}' (LLM resolved)",
                check=False, timeout=30,
            )
        except subprocess.TimeoutExpired:
            self._log(f"git commit timed out for branch '{branch}'")
            abort_ok = self._abort_merge()
            if not abort_ok:
                report.failure_reason = "merge_abort_failed"
                if self.strategy == MergeStrategy.FAST:
                    return "fast_abort"
                return "merge_abort_failed"
            report.failure_reason = "resolution_commit_timeout"
            if self.strategy == MergeStrategy.FAST:
                return "fast_abort"
            return "resolution_commit_timeout"
        if commit_result.returncode != 0:
            self._log(f"Merge commit failed: {commit_result.stderr.strip()}")
            abort_ok = self._abort_merge()
            if not abort_ok:
                report.failure_reason = "merge_abort_failed"
                if self.strategy == MergeStrategy.FAST:
                    return "fast_abort"
                return "merge_abort_failed"
            report.failure_reason = "resolution_commit_failed"
            if self.strategy == MergeStrategy.FAST:
                return "fast_abort"
            return "resolution_commit_failed"

        # Run guardrails
        try:
            post_merge_sha = _run_git(
                self.project_root, "rev-parse", "HEAD",
                check=False, timeout=15,
            ).stdout.strip()
        except subprocess.TimeoutExpired:
            self._log(
                "git rev-parse HEAD timed out after merge commit for '%s'. "
                "Cannot verify post-merge SHA; guardrails will fail closed.",
                branch,
            )
            post_merge_sha = ""
        try:
            guardrails_result = self._run_guardrails(
                pre_merge_sha, post_merge_sha, branch, strategy=self.strategy,
            )
        except GuardrailRepairFailed as exc:
            if exc.failure_reason == "guardrail_check_failed":
                self._log(
                    f"Guardrails check itself crashed for '{branch}' in fast mode: {exc}"
                )
            else:
                self._log(
                    f"Guardrail repair failed for '{branch}' in fast mode: {exc}"
                )
            report.rollback_failed = getattr(exc, "rollback_failed", False)
            report.failure_reason = exc.failure_reason
            return "fast_abort"
        except GuardrailCallFileError as exc:
            self._log(
                f"Guardrail violation detected, rollback succeeded, but "
                f"call file could not be written: {exc}"
            )
            report.rollback_failed = False
            report.failure_reason = "guardrail_violation_call_failed"
            return "guardrail_violation_call_failed"
        except GuardrailNoRollbackError as exc:
            self._log(
                f"Guardrails check for '{branch}' failed: {exc}. "
                f"Rollback was not attempted because pre_merge_sha was missing. "
                f"The merge commit may still be in HEAD."
            )
            report.rollback_failed = False
            if exc.call_file is not None:
                report.human_call_file = exc.call_file
            return "guardrail_violation_no_rollback"
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            self._log(f"Rollback failed after guardrail violation: {exc}")
            report.rollback_failed = True
            if hasattr(exc, "call_file") and exc.call_file is not None:
                report.human_call_file = exc.call_file
            return "rollback_failed"
        if guardrails_result is not None:
            report.human_call_file = guardrails_result
            return "guardrail_violation"

        # Refresh SHA in case guardrail repair amended the commit
        sha_fresh = True
        try:
            post_merge_sha = _run_git(
                self.project_root, "rev-parse", "HEAD",
                check=False, timeout=15,
            ).stdout.strip()
        except subprocess.TimeoutExpired:
            self._log(
                f"git rev-parse HEAD timed out after guardrails for '{branch}'. "
                "SHA may be stale."
            )
            sha_fresh = False
            # Clear the stale value so the log does not show a misleading SHA
            post_merge_sha = "<unavailable — refresh timed out>"
        sha_note = "" if sha_fresh else " (SHA may be stale)"
        self._log(
            f"LLM-resolved merge of '{branch}' committed successfully "
            f"(SHA: {post_merge_sha}){sha_note}"
        )
        return "merged"

    def _run_guardrails(
        self,
        pre_sha: str,
        post_sha: str,
        branch: str,
        strategy: MergeStrategy = MergeStrategy.DEFAULT,
    ) -> Optional[Path]:
        """Run guardrails check on spec files changed in the merge.

        If violations are found or the check itself fails, rolls back to
        ``pre_sha`` BEFORE writing the human call file so the call file's
        message is always truthful.

        In ``fast`` strategy, violations are fed to the LLM for repair instead
        of escalating to a human call. If repair succeeds, the merge commit is
        amended and ``None`` is returned. If repair fails,
        ``GuardrailRepairFailed`` is raised (after rollback).

        Args:
            pre_sha: SHA of HEAD before the merge.
            post_sha: SHA of the merge commit.
            branch: The branch being merged.
            strategy: The merge strategy tier.

        Returns:
            ``None`` if guardrails passed (or were repaired in fast mode).
            ``Path`` to the human call file if violations were found or the
            check itself failed (rollback performed, human call written).
            Only returned for ``default`` and ``strict`` strategies.

        Raises:
            GuardrailRepairFailed: In ``fast`` strategy, when LLM repair of
            guardrail violations fails after rollback.
            RuntimeError: If the rollback (git reset --hard) fails. The
            caller must escalate because the tree is in an inconsistent state.
        """
        if not pre_sha or not post_sha:
            logger.warning(
                "Guardrails check skipped for '%s': missing pre/post SHA "
                "(pre_sha=%r, post_sha=%r). Treating as failure.",
                branch, pre_sha, post_sha,
            )

            # Fast mode: abort immediately without human call (no rollback needed
            # when SHA is missing — there is no known good state to roll back to)
            if strategy == MergeStrategy.FAST:
                if not pre_sha and not post_sha:
                    missing_reason = "pre and post SHA"
                    failure_reason = "guardrail_missing_pre_and_post_sha"
                elif not pre_sha:
                    missing_reason = "pre_sha"
                    failure_reason = "guardrail_missing_pre_sha"
                else:
                    missing_reason = "post_sha"
                    failure_reason = "guardrail_missing_post_sha"
                raise GuardrailRepairFailed(
                    f"Guardrails check skipped for '{branch}': missing {missing_reason} "
                    f"(pre_sha={pre_sha!r}, post_sha={post_sha!r}). "
                    f"Fast mode aborts without human call.",
                    failure_reason=failure_reason,
                    rollback_failed=False,
                )

            # Non-fast: attempt rollback if pre_sha exists
            if pre_sha:
                try:
                    self._rollback_to(pre_sha)
                except (RuntimeError, subprocess.TimeoutExpired) as rbe:
                    call_message = (
                        f"Guardrails check skipped: missing SHA "
                        f"(pre_sha={pre_sha!r}, post_sha={post_sha!r}). "
                        f"Rollback also failed: {rbe}."
                    )
                    self._log(call_message)
                    raise RuntimeError(call_message) from rbe

            call_message = (
                f"Guardrails check skipped: missing SHA "
                f"(pre_sha={pre_sha!r}, post_sha={post_sha!r})."
            )
            if not pre_sha:
                call_message += (
                    " NOTE: could not roll back because pre_merge_sha was also "
                    "missing. The merge commit may still be in HEAD."
                )
            try:
                call_file = self._human_writer.write_guardrail_call(
                    branch=branch,
                    violations=[
                        {
                            "file_path": "N/A",
                            "violation_type": "MISSING_SHA",
                            "message": call_message,
                        }
                    ],
                    pre_merge_sha=pre_sha,
                )
            except Exception as exc:
                self._log(f"Failed to write guardrail human call file: {exc}")
                if pre_sha:
                    call_err_msg = (
                        f"Guardrails failed for '{branch}' (missing SHA) and the "
                        f"human call file could not be written: {exc}. "
                        f"The merge has been rolled back; manual intervention required."
                    )
                else:
                    call_err_msg = (
                        f"Guardrails failed for '{branch}' (missing SHA) and the "
                        f"human call file could not be written: {exc}. "
                        f"Rollback was NOT attempted because pre_merge_sha was missing. "
                        f"The merge commit may still be in HEAD."
                    )
                raise GuardrailCallFileError(call_err_msg) from exc
            try:
                self._human_writer.print_instructions(call_file)
            except Exception as exc:
                self._log(
                    f"WARNING: Failed to print instructions (call file was written "
                    f"successfully): {exc}"
                )
            if not pre_sha:
                raise GuardrailNoRollbackError(
                    f"Guardrails check for '{branch}' could not roll back because "
                    f"pre_merge_sha was missing. The merge commit may still be in HEAD.",
                    call_file=call_file,
                )
            return call_file
        try:
            gr_report = self._guardrails.check_merge_result(pre_sha, post_sha)
            if gr_report.passed:
                self._log(f"Guardrails passed for merge of '{branch}'")
                return None

            self._log(
                f"Guardrails detected {len(gr_report.violations)} violation(s) "
                f"for '{branch}' (reason: post-merge guardrails violation)"
            )
            for v in gr_report.violations:
                self._log(f"  [{v.violation_type}] {v.file_path}: {v.message}")

            # --- fast strategy: attempt LLM repair ---
            if strategy == MergeStrategy.FAST:
                self._log(
                    f"Fast strategy: attempting LLM repair of "
                    f"{len(gr_report.violations)} guardrail violation(s)"
                )

                # Gather original and merged spec contents for the repair prompt
                spec_files = _get_changed_spec_files(
                    self.project_root, pre_sha, post_sha,
                )
                original_specs: dict[str, str] = {}
                merged_specs: dict[str, str] = {}
                for sp in spec_files:
                    orig = _read_file_from_ref(self.project_root, sp, pre_sha)
                    merged = _read_file_from_ref(self.project_root, sp, post_sha)
                    if orig is None:
                        self._log(
                            f"WARNING: Could not read original content of {sp} "
                            f"from ref {pre_sha} — including placeholder in repair prompt"
                        )
                        orig = f"[Content unavailable at ref {pre_sha}]"
                    if merged is None:
                        self._log(
                            f"WARNING: Could not read merged content of {sp} "
                            f"from ref {post_sha} — including placeholder in repair prompt"
                        )
                        merged = f"[Content unavailable at ref {post_sha}]"
                    original_specs[sp] = orig
                    merged_specs[sp] = merged

                repair_result = self._repairer.repair_violations(
                    branch=branch,
                    pre_sha=pre_sha,
                    post_sha=post_sha,
                    violations=gr_report.violations,
                    original_spec_contents=original_specs,
                    merged_spec_contents=merged_specs,
                )

                if repair_result.success:
                    self._log(
                        f"Guardrail repair succeeded for '{branch}': "
                        f"{len(repair_result.repaired_files)} file(s) corrected"
                    )
                    return None

                # Repair failed — rollback and abort (no human call in fast)
                self._log(
                    f"Guardrail repair failed for '{branch}': {repair_result.error}"
                )
                try:
                    self._rollback_to(pre_sha)
                except (RuntimeError, subprocess.TimeoutExpired) as rbe:
                    raise GuardrailRepairFailed(
                        f"Guardrail repair failed for '{branch}': {repair_result.error}. "
                        f"Rollback also failed: {rbe}",
                        failure_reason="guardrail_repair_failed",
                        rollback_failed=True,
                    ) from rbe
                raise GuardrailRepairFailed(
                    f"Guardrail repair failed for '{branch}': {repair_result.error}",
                    failure_reason="guardrail_repair_failed",
                )

            # --- default / strict strategy: rollback + human call ---
            try:
                self._rollback_to(pre_sha)
            except (RuntimeError, subprocess.TimeoutExpired) as rbe:
                self._log(
                    f"Rollback failed after guardrail violation for "
                    f"'{branch}': {rbe}"
                )
                # Preserve actual violation details in the human call file
                # even when rollback fails, so the operator knows what was
                # weakened before the tree got into an inconsistent state.
                violation_dicts = self._violations_to_dicts(gr_report.violations)
                call_file: Optional[Path] = None
                try:
                    call_file = self._human_writer.write_guardrail_call(
                        branch=branch,
                        violations=violation_dicts,
                        pre_merge_sha=pre_sha,
                    )
                except Exception as write_exc:
                    self._log(
                        f"Failed to write guardrail human call file: {write_exc}"
                    )
                    raise GuardrailRollbackError(
                        f"Guardrails detected {len(gr_report.violations)} "
                        f"violation(s) for '{branch}' but rollback failed: {rbe}. "
                        f"Additionally, the human call file could not be written. "
                        f"Working tree may be in an inconsistent state. "
                        f"Manual intervention required.",
                        call_file=None,
                    ) from write_exc
                raise GuardrailRollbackError(
                    f"Guardrails detected {len(gr_report.violations)} "
                    f"violation(s) for '{branch}' but rollback failed: {rbe}. "
                    f"Working tree may be in an inconsistent state. "
                    f"Manual intervention required.",
                    call_file=call_file,
                ) from rbe

            call_file = None
            try:
                violation_dicts = self._violations_to_dicts(gr_report.violations)
                call_file = self._human_writer.write_guardrail_call(
                    branch=branch,
                    violations=violation_dicts,
                    pre_merge_sha=pre_sha,
                )
            except Exception as exc:
                self._log(f"Failed to write guardrail human call file: {exc}")
                raise GuardrailCallFileError(
                    f"Guardrails detected violations for '{branch}' and the "
                    f"human call file could not be written: {exc}. "
                    f"The merge has been rolled back; manual intervention required."
                ) from exc
            try:
                self._human_writer.print_instructions(call_file)
            except Exception as exc:
                self._log(
                    f"WARNING: Failed to print instructions (call file was written "
                    f"successfully): {exc}"
                )
            return call_file
        except GuardrailRepairFailed:
            raise  # re-raise fast-mode repair failures without wrapping
        except Exception as exc:
            self._log(f"Guardrails check failed for '{branch}': {exc}")
            if strategy == MergeStrategy.FAST:
                # In fast mode the guardrail check crash is the primary failure;
                # attempt rollback and surface rollback failure in the reason so
                # the CLI can distinguish a simple check crash from a corrupted
                # working tree.
                rollback_ok = True
                try:
                    self._rollback_to(pre_sha)
                except (RuntimeError, subprocess.TimeoutExpired) as rbe:
                    self._log(
                        f"Rollback also failed after guardrails check crash: {rbe}"
                    )
                    rollback_ok = False
                failure_reason = (
                    "guardrail_check_failed_and_rollback_failed"
                    if not rollback_ok else "guardrail_check_failed"
                )
                raise GuardrailRepairFailed(
                    f"Guardrails check itself crashed for '{branch}': {exc}. "
                    f"No repair was attempted. Fast mode aborts without human call.",
                    failure_reason=failure_reason,
                    rollback_failed=not rollback_ok,
                ) from exc
            try:
                self._rollback_to(pre_sha)
            except (RuntimeError, subprocess.TimeoutExpired) as rbe:
                self._log(
                    f"Rollback also failed after guardrails check crash: {rbe}"
                )
                raise GuardrailRollbackError(
                    f"Guardrails check itself crashed for '{branch}': {exc}. "
                    f"Rollback also failed: {rbe}. "
                    f"Working tree may be in an inconsistent state. "
                    f"Manual intervention required.",
                ) from exc
            try:
                call_file = self._human_writer.write_guardrail_call(
                    branch=branch,
                    violations=[
                        {
                            "file_path": "N/A",
                            "violation_type": "CHECK_FAILURE",
                            "message": f"Guardrails check raised an exception: {exc}",
                        }
                    ],
                    pre_merge_sha=pre_sha,
                )
            except Exception as write_exc:
                self._log(f"Failed to write guardrail human call file: {write_exc}")
                raise GuardrailCallFileError(
                    f"Guardrails check failed for '{branch}' and the "
                    f"human call file could not be written: {write_exc}. "
                    f"The merge has been rolled back; manual intervention required."
                ) from write_exc
            try:
                self._human_writer.print_instructions(call_file)
            except Exception as print_exc:
                self._log(
                    f"WARNING: Failed to print instructions (call file was written "
                    f"successfully): {print_exc}"
                )
            return call_file

    def _abort_merge(self) -> bool:
        """Abort the current merge to restore working tree.

        Returns:
            True if abort succeeded, False if it failed.
        """
        try:
            abort_result = _run_git(
                self.project_root,
                "merge",
                "--abort",
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            self._log(f"git merge --abort timed out: {exc}")
            return False
        if abort_result.returncode == 0:
            self._log("git merge --abort succeeded")
            return True
        else:
            self._log(f"git merge --abort failed: {abort_result.stderr.strip()}")
            return False

    @staticmethod
    def _violations_to_dicts(violations: list) -> list[dict]:
        """Convert a list of GuardrailViolation objects to plain dicts.

        Centralised so that rollback-failure and rollback-success paths in
        ``_run_guardrails`` stay consistent when the data model changes.
        """
        return [
            {
                "file_path": v.file_path,
                "violation_type": v.violation_type,
                "message": v.message,
            }
            for v in violations
        ]

    def _rollback_to(self, sha: str) -> None:
        """Hard reset to a previous SHA to undo a merge commit.

        Raises:
            RuntimeError: If git reset --hard fails or times out. Callers
            must escalate because the working tree is in an inconsistent state.
        """
        if not sha:
            return
        try:
            reset_result = _run_git(
                self.project_root,
                "reset",
                "--hard",
                sha,
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            self._log(f"git reset --hard {sha} timed out: {exc}")
            raise RuntimeError(
                f"git reset --hard {sha} timed out: {exc}. "
                f"Working tree may be in an inconsistent state. "
                f"Manual intervention required."
            ) from exc
        if reset_result.returncode == 0:
            self._log(f"git reset --hard {sha} succeeded — merge rolled back")
        else:
            error_msg = reset_result.stderr.strip()
            self._log(f"git reset --hard failed: {error_msg}")
            raise RuntimeError(
                f"git reset --hard {sha} failed: {error_msg}. "
                f"Working tree may be in an inconsistent state. "
                f"Manual intervention required."
            )
