"""MergeOrchestrator — Sequential merge of branches into current branch.

Orchestrates the merge flow: for each branch, call git merge, handle
clean merge / conflict / non-conflict-failure, run guardrails, and
aggregate results.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..llm_caller import LLMCaller
from ..version_bumper import BumpType, Version
from ..worktree import _run_git, get_conflicting_files, get_current_branch
from ...commands.merge.failure_reason import FailureReason
from ...commands.merge.merge_lock import MergeLock, MergeLockBusy, MergeLockStale
from ...commands.merge.postcondition import (
    PostConditionViolated,
    assert_branch_merged,
    assert_head_is_merge_commit,
)
from .cleanup import CleanupManager, CleanupReport
from .conflict_context import build as build_conflict_context
from .conflict_resolver import ConflictResolver, LLMResolution, MergeStrategy
from .guardrail_repair import GuardrailRepairer
from .guardrails import (
    MergeGuardrailsCheck,
    _get_changed_spec_files,
    _read_file_from_ref,
    violation_set_hash,
)
from .human_call import HumanCallWriter
from .runtime_sync import (
    DEST_HASH_UNAVAILABLE,
    BypassedCollision,
    RuntimeSyncCollision,
    sync_branch_runtime,
)
from .strategy import DecisionAction, StrategyDecider, StrategyDecision
from .version_aggregator import (
    InferResult,
    aggregate_and_apply,
    infer_branch_bump,
    read_version_at_ref,
)

logger = logging.getLogger(__name__)

# Default maximum LLM repair iterations in fast mode before giving up.
# Stall is detected when two *consecutive repair-iteration* hashes match.
# The initial gr_report hash is intentionally NOT compared against
# last_hash (initialised to None), so a true stall requires
# iter1_hash == iter2_hash, which is detected on iteration 2.
_DEFAULT_MAX_REPAIR_ITERATIONS = 2


def _load_max_repair_iterations(project_root: Path) -> int:
    """Read max repair iterations from se3.yaml, with safe fallback.

    Looks under ``merge.guardrail_repair.max_iterations``.
    Invalid or missing values fall back to the default.
    """
    from ...config import load_project_yaml

    try:
        data, _src = load_project_yaml(project_root)
    except Exception:
        return _DEFAULT_MAX_REPAIR_ITERATIONS
    if not data:
        return _DEFAULT_MAX_REPAIR_ITERATIONS
    merge_data = data.get("merge", {})
    if not isinstance(merge_data, dict):
        return _DEFAULT_MAX_REPAIR_ITERATIONS
    gr_data = merge_data.get("guardrail_repair", {})
    if not isinstance(gr_data, dict):
        return _DEFAULT_MAX_REPAIR_ITERATIONS
    raw = gr_data.get("max_iterations")
    if raw is None:
        return _DEFAULT_MAX_REPAIR_ITERATIONS
    try:
        val = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "merge.guardrail_repair.max_iterations=%r is not a valid integer; "
            "using default %d",
            raw, _DEFAULT_MAX_REPAIR_ITERATIONS,
        )
        return _DEFAULT_MAX_REPAIR_ITERATIONS
    if val < 1:
        logger.warning(
            "merge.guardrail_repair.max_iterations=%d must be >= 1; "
            "using default %d",
            val, _DEFAULT_MAX_REPAIR_ITERATIONS,
        )
        return _DEFAULT_MAX_REPAIR_ITERATIONS
    return val


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


class GuardrailRepairStalled(RuntimeError):
    """Raised when fast-mode guardrail repair makes no progress.

    After LLM repair, if the violation set hash is unchanged for
    consecutive iterations, we stop retrying and escalate to a human
    call instead of aborting.
    """

    def __init__(
        self,
        message: str,
        call_file: Optional[Path] = None,
        iteration_count: int = 0,
        last_violation_hash: str = "",
        failure_reason: str = "guardrail_repair_stalled",
    ) -> None:
        super().__init__(message)
        self.call_file = call_file
        self.iteration_count = iteration_count
        self.last_violation_hash = last_violation_hash
        self.failure_reason = failure_reason


class GuardrailRepairExhausted(GuardrailRepairStalled):
    """Raised when fast-mode guardrail repair reaches max iterations.

    Subclass of GuardrailRepairStalled so callers can distinguish the
    exhausted path (max iterations reached without resolution, hash
    kept changing) from the stalled path (consecutive identical hashes)
    while still catching both with a single ``except GuardrailRepairStalled``.
    """

    def __init__(
        self,
        message: str,
        call_file: Optional[Path] = None,
        iteration_count: int = 0,
        last_violation_hash: str = "",
    ) -> None:
        super().__init__(
            message,
            call_file=call_file,
            iteration_count=iteration_count,
            last_violation_hash=last_violation_hash,
            failure_reason="guardrail_repair_exhausted",
        )


@dataclass
class MergeReport:
    """Result of a merge orchestration run."""

    success: bool = False
    # NOTE: On a ``runtime_sync_collision`` halt in strict mode, the
    # failed branch appears in BOTH ``merged_branches`` (the git merge
    # commit is on HEAD) and ``failed_branch``. Programmatic consumers
    # iterating ``merged_branches`` and assuming success may double-count
    # the colliding branch; always pair with ``failure_reason`` checks.
    merged_branches: list[str] = field(default_factory=list)
    # Task 17 / B10: typed split of ``merged_branches`` into three
    # semantically-distinct buckets. ``merged_branches`` is preserved as
    # the union for backward compatibility; new consumers SHOULD prefer
    # the typed buckets.
    newly_merged_branches: list[str] = field(default_factory=list)
    already_ancestor_branches: list[str] = field(default_factory=list)
    branches_with_warnings: list[str] = field(default_factory=list)
    failed_branch: Optional[str] = None
    failure_reason: Optional[str] = None
    # Task 17 / B9: typed reason enum (parallel to the legacy string).
    typed_failure_reason: Optional[FailureReason] = None
    pending_human: bool = False
    human_call_file: Optional[Path] = None
    log_file: Optional[Path] = None
    pre_merge_version: Optional[str] = None
    effective_pre_merge_version: Optional[str] = None
    final_version: Optional[str] = None
    bump_type: Optional[str] = None
    version_aggregation_skipped: bool = False
    version_aggregation_error: Optional[str] = None
    cleanup_report: Optional[CleanupReport] = None
    cleanup_skipped: bool = True
    runtime_sync_skipped_branches: list[str] = field(default_factory=list)
    runtime_sync_skipped_files: list[tuple[str, list[str]]] = field(default_factory=list)
    runtime_sync_discarded: list[tuple[str, list[str]]] = field(default_factory=list)
    runtime_sync_collisions: list[BypassedCollision] = field(default_factory=list)
    runtime_sync_collision_path: Optional[str] = None
    # Per-branch count of idempotent sidecar bypasses (sidecar file already
    # existed with content matching source). Surfaced as a weak signal so a
    # stale sidecar from a prior aborted run is not invisible to operators
    # inheriting the worktree.
    runtime_sync_idempotent_bypasses: list[tuple[str, int]] = field(default_factory=list)
    # Per-file audit detail (BypassedCollision records) of idempotent
    # bypasses, parallel to ``runtime_sync_collisions``. Operators
    # investigating a stale-sidecar warning can inspect this list to see the
    # exact sidecar paths without rerunning under DEBUG logging. The rendered
    # ``se3 merge`` output continues to surface only the per-branch count via
    # ``runtime_sync_idempotent_bypasses`` to keep the audit-noise concern
    # contained to programmatic consumers.
    runtime_sync_idempotent_records: list[BypassedCollision] = field(default_factory=list)
    rollback_failed: bool = False
    unattempted_branches: list[str] = field(default_factory=list)

    def set_failure_reason(self, reason: str | FailureReason | None) -> None:
        """Set both the legacy string and the typed-enum failure reason.

        Task 17 / B8: Centralises failure_reason assignments so that the
        typed enum stays in sync with the legacy string. Accepts either a
        string (parsed via :func:`from_legacy_string`) or a
        :class:`FailureReason` directly. Compound prefixes (``fast_abort:
        ...``) preserve their detail in the legacy string while the
        typed enum captures only the base reason.
        """
        from ...commands.merge.failure_reason import (
            from_legacy_string,
            to_legacy_string,
        )
        if reason is None:
            self.failure_reason = None
            self.typed_failure_reason = None
        elif isinstance(reason, FailureReason):
            self.typed_failure_reason = reason
            self.failure_reason = to_legacy_string(reason) or None
        else:
            self.failure_reason = reason
            typed, _detail = from_legacy_string(reason)
            self.typed_failure_reason = typed


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
        strict_runtime_sync: bool = False,
    ) -> None:
        self.project_root = project_root
        if strategy not in ("default", "strict", "fast"):
            raise ValueError(
                f"Unknown merge strategy: {strategy!r}. "
                f"Must be one of: default, strict, fast"
            )
        self.strategy = MergeStrategy(strategy)
        self.delete_merged = delete_merged
        self.strict_runtime_sync = strict_runtime_sync
        self.log_file: Optional[Path] = None
        self._log_lines: list[str] = []
        self._resolver = ConflictResolver(project_root)
        self._decider = StrategyDecider()
        self._human_writer = HumanCallWriter(project_root)
        self._guardrails = MergeGuardrailsCheck(project_root)
        # Task 12 / A13: shared LLMCaller for prompt cache reuse across
        # conflict resolution and guardrail repair.
        self._llm_caller = LLMCaller(
            project_root=project_root,
            step_type="guardrail_repair",
            max_retries=2,
            retry_delay=1.0,
        )
        self._repairer = GuardrailRepairer(
            project_root,
            llm_caller=self._llm_caller,
        )
        self._max_repair_iterations = _load_max_repair_iterations(project_root)
        self._last_stall_iteration_count: Optional[int] = None
        # Set by _merge_single_branch when guardrail repair changed HEAD
        # (e.g. fix-up commit or amend).  _record_merged uses it to skip
        # assert_head_is_merge_commit in that case.
        self._last_merge_repair_changed_head: bool = False

    def _log(self, message: str) -> None:
        """Append a line to the internal log buffer and the logger."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}"
        self._log_lines.append(line)
        logger.info(message)

    def _write_log(self) -> None:
        """Flush the log buffer to se3/logs/merge_<ts>.log.

        Task 19 / B13: write+fsync so that a crash mid-flush does not
        leave a truncated or zero-length log file. We write through a
        raw file descriptor (rather than ``Path.write_text``) so we
        can call ``os.fsync`` on the data and the parent directory.
        """
        logs_dir = self.project_root / "se3" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.log_file = logs_dir / f"merge_{ts}.log"
        payload = "\n".join(self._log_lines) + "\n"
        encoded = payload.encode("utf-8")
        fd = os.open(
            str(self.log_file),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o644,
        )
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        # fsync the directory so the dirent is durable.
        try:
            dir_fd = os.open(str(logs_dir), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Some filesystems do not support directory fsync (e.g. tmpfs).
            # The data fsync above is the load-bearing call; absorb a
            # missing-feature failure rather than aborting the merge.
            pass

    def _populate_unattempted(self, report: MergeReport, branches: list[str]) -> None:
        """Compute unattempted branches when the loop exits early and log them."""
        if report.failed_branch and report.failed_branch in branches:
            failed_idx = branches.index(report.failed_branch)
            report.unattempted_branches = branches[failed_idx + 1:]
            if report.unattempted_branches:
                self._log(
                    f"Unattempted branches: {', '.join(report.unattempted_branches)}"
                )

    def _is_fast_forward(self, pre_merge_sha: str) -> bool:
        """Check whether the current HEAD is a fast-forward from *pre_merge_sha*.

        A fast-forward (or fix-up commit on top of a merge) has the
        property that ``HEAD^1 == pre_merge_sha``.  This is used by
        :meth:`_record_merged` to skip the merge-commit post-condition
        when git did not create a merge commit.
        """
        if not pre_merge_sha:
            return False
        try:
            head_parent = _run_git(
                self.project_root,
                "rev-parse", "--verify", "HEAD^1",
                check=False, timeout=15,
            )
        except subprocess.TimeoutExpired:
            return False
        if head_parent.returncode != 0:
            return False
        return head_parent.stdout.strip() == pre_merge_sha

    def _record_merged(
        self,
        report: MergeReport,
        branch: str,
        already_ancestor: bool,
        warnings_repaired: bool = False,
        pre_merge_sha: str = "",
    ) -> Optional[str]:
        """Record a merged branch and run post-conditions.

        Task 13 / B1: every "merged" boundary must verify, before being
        recorded as success, that:

          * the branch is actually an ancestor of HEAD; and
          * for non-no-op merges, HEAD is itself a merge commit.

        The merge-commit check is skipped when:
          * guardrail repair created a fix-up commit
            (``self._last_merge_repair_changed_head``), or
          * the merge was a fast-forward (``HEAD^1 == pre_merge_sha``).

        Returns ``None`` on success, or a failure-reason string when a
        post-condition fails. The caller is expected to map this string
        through the same state-machine arms as other failure paths.

        Task 17 / B10: also populates the typed buckets
        (``newly_merged_branches`` / ``already_ancestor_branches`` /
        ``branches_with_warnings``) in addition to the legacy
        ``merged_branches`` field.
        """
        skip_merge_commit_check = (
            self._last_merge_repair_changed_head
            or self._is_fast_forward(pre_merge_sha)
        )
        try:
            assert_branch_merged(self.project_root, branch)
            if not already_ancestor and not skip_merge_commit_check:
                assert_head_is_merge_commit(self.project_root, branch)
        except PostConditionViolated as pcv:
            self._log(
                f"Post-condition failed for '{branch}': {pcv.detail}"
            )
            report.set_failure_reason(pcv.reason)
            report.failed_branch = branch
            return pcv.reason.legacy_string

        report.merged_branches.append(branch)
        if already_ancestor:
            report.already_ancestor_branches.append(branch)
        elif warnings_repaired:
            report.branches_with_warnings.append(branch)
        else:
            report.newly_merged_branches.append(branch)
        return None

    def _aggregate_versions(
        self,
        report: MergeReport,
        branch_bumps: list[BumpType],
        effective_pre_merge_version: Optional[str],
    ) -> None:
        """Apply SemVer aggregation if any merges contributed bumps.

        Task 18 / B12: extracted from ``execute()`` so that it can be
        called from runtime-sync failure paths too. A merge commit that
        landed on HEAD must contribute to the version even if a later
        non-git step (runtime sync, cleanup) fails.

        Idempotent: callers may invoke multiple times safely; this
        method short-circuits when ``report.final_version`` is already
        populated.
        """
        if report.final_version is not None:
            return
        if effective_pre_merge_version and branch_bumps:
            report.effective_pre_merge_version = effective_pre_merge_version
            self._log("Aggregating SemVer bumps from merged branches")
            try:
                is_published = self._is_head_published()
                if is_published:
                    self._log(
                        "WARNING: HEAD has been published to a remote. "
                        "Creating a new commit for version aggregation instead of amending."
                    )
                agg = aggregate_and_apply(
                    self.project_root,
                    branch_bumps,
                    effective_pre_merge_version,
                    amend=not is_published,
                )
                if agg.success:
                    report.final_version = agg.new_version
                    if agg.bump_type is not None:
                        report.bump_type = agg.bump_type.value
                    report.version_aggregation_skipped = False
                    self._log(
                        f"Version aggregated: {effective_pre_merge_version} → {agg.new_version} "
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
            if not effective_pre_merge_version:
                self._log("Skipping version aggregation: no pre-merge version available")
            elif not branch_bumps:
                self._log("Skipping version aggregation: no branches contributed bumps")

    def _infer_bump_for_branch(
        self,
        branch: str,
        pre_merge_sha: str,
    ) -> Optional[BumpType]:
        """Infer the SemVer bump produced by ``branch`` end-to-end.

        Task 18 helper: factors the merge-base + ``infer_branch_bump``
        sequence so it can be called from the normal post-merge path AND
        from runtime-sync failure paths (where the branch is merged at
        the git level even though the sync afterwards failed).

        Returns ``None`` if the bump cannot be inferred.
        """
        try:
            mb = _run_git(
                self.project_root,
                "merge-base", pre_merge_sha, branch,
                check=False, timeout=15,
            )
        except subprocess.TimeoutExpired as exc:
            self._log(
                f"merge-base timed out for '{branch}': {exc} — "
                f"skipping bump inference"
            )
            return None
        if mb.returncode != 0:
            self._log(
                f"merge-base failed for '{branch}': "
                f"{mb.stderr.strip()} — skipping bump inference"
            )
            return None
        try:
            infer_result = infer_branch_bump(
                self.project_root, branch, mb.stdout.strip(),
            )
        except Exception as exc:
            self._log(f"Failed to infer bump for '{branch}': {exc}")
            return None
        if infer_result.bump is None:
            self._log(
                f"Bump inference skipped for '{branch}': {infer_result.reason}"
            )
            return None
        return infer_result.bump

    def execute(self, branches: list[str]) -> MergeReport:
        """Execute sequential merge of all branches.

        Args:
            branches: Branch names to merge, in order.

        Returns:
            MergeReport summarizing the outcome.
        """
        # Task 20: acquire the merge lock first thing so that a concurrent
        # ``se3 merge`` invocation fails immediately with a typed error
        # rather than racing on the working tree. The lock is released when
        # this method returns (success or failure).
        merge_lock: Optional[MergeLock] = None
        try:
            merge_lock = MergeLock(self.project_root)
            merge_lock.acquire()
        except (MergeLockBusy, MergeLockStale) as exc:
            report = MergeReport()
            report.set_failure_reason(FailureReason.LOCK_BUSY)
            report.failure_reason = "lock_busy"
            report.failed_branch = None
            self._log(f"Merge lock contention: {exc}")
            self._write_log()
            report.log_file = self.log_file
            report.unattempted_branches = list(branches)
            return report

        try:
            return self._execute_locked(branches)
        finally:
            if merge_lock is not None:
                merge_lock.release()

    def _execute_locked(self, branches: list[str]) -> MergeReport:
        """Body of :meth:`execute` that runs while the merge lock is held."""
        report = MergeReport()
        current_branch = get_current_branch(self.project_root)
        self._current_branch = current_branch

        # Defensive: clear stale instance state from any prior execution so
        # that a later merge that stalls without setting this attribute does
        # not pick up the value from a previous stall.
        self._last_stall_iteration_count = None

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
        # Track the effective pre-merge version for aggregation. When
        # already-merged branches have their version changes already in HEAD,
        # this is updated to the version before those branches (so retries
        # after partial failures do not over-bump).
        effective_pre_merge_version = pre_merge_version

        for idx, branch in enumerate(branches):
            # Reset per-branch mutable state so a stall on an earlier branch
            # does not leak into a later branch's result formatting.
            self._last_stall_iteration_count = None
            self._last_merge_repair_changed_head = False
            self._log(f"--- Merging branch: {branch} ---")

            result = self._merge_single_branch(branch, report)

            if result == "merged" or result == "already_merged":
                self._log(f"Branch '{branch}' merged successfully")
                # Task 13 / B1: post-condition checks before recording
                # the branch as merged. _record_merged returns a failure
                # reason on violation; treat it as a fatal post-condition
                # failure that halts the sequence (the merge state is
                # ambiguous so we cannot safely continue).
                pcv_reason = self._record_merged(
                    report, branch,
                    already_ancestor=(result == "already_merged"),
                    warnings_repaired=False,
                    pre_merge_sha=pre_merge_sha,
                )
                if pcv_reason is not None:
                    self._log(
                        f"Branch '{branch}' post-condition violated: {pcv_reason} — "
                        f"halting sequence"
                    )
                    if report.merged_branches:
                        self._log(
                            f"Version not bumped despite "
                            f"{len(report.merged_branches)} successful merge(s) — "
                            f"re-run after resolving"
                        )
                    report.success = False
                    report.failed_branch = branch
                    if not report.failure_reason:
                        report.set_failure_reason(pcv_reason)
                    report.version_aggregation_skipped = True
                    self._populate_unattempted(report, branches)
                    self._write_log()
                    report.log_file = self.log_file
                    return report

                if not pre_merge_sha or not pre_merge_version:
                    # No pre-merge version snapshot — cannot infer bump.
                    continue

                if result == "already_merged":
                    # Task 14 / B2,B3,B4: already-merged branches do NOT
                    # contribute to bump inference. The version on HEAD
                    # already reflects whatever the branch contained;
                    # re-inferring would risk double-counting. The log
                    # message and behaviour are now consistent: we say
                    # "skipping bump inference" AND we actually skip it.
                    self._log(
                        f"Already-merged branch '{branch}' — "
                        f"skipping bump inference (its version change is "
                        f"already in HEAD)"
                    )
                    # However, we still need to adjust
                    # effective_pre_merge_version so that newly-merged
                    # branches in the same invocation are aggregated
                    # against the correct base.  Find the version at the
                    # state before this branch was merged and use the
                    # lowest such version as the effective base.
                    base_ref, _merged_commit = self._find_base_ref_for_already_merged(
                        branch, pre_merge_sha,
                    )
                    if base_ref is not None:
                        base_version = read_version_at_ref(
                            self.project_root, base_ref,
                        )
                        if base_version is not None:
                            try:
                                base_v = Version.parse(base_version)
                                eff_v = (
                                    Version.parse(effective_pre_merge_version)
                                    if effective_pre_merge_version
                                    else None
                                )
                                if eff_v is None or base_v < eff_v:
                                    effective_pre_merge_version = base_version
                                    self._log(
                                        f"Effective pre-merge version adjusted to "
                                        f"{base_version} (before '{branch}' was merged)"
                                    )
                            except ValueError:
                                pass
                    continue

                # Newly-merged branch: compute end-to-end diff bump.
                base_ref = pre_merge_sha
                merged_commit: Optional[str] = None

                try:
                    merge_base_result = _run_git(
                        self.project_root,
                        "merge-base",
                        base_ref,
                        branch,
                        check=False,
                        timeout=15,
                    )
                except subprocess.TimeoutExpired as exc:
                    # Task 15 / B6: preserve the original timeout info in
                    # the log so operators can correlate with system
                    # metrics; do not silently drop the exception.
                    self._log(
                        f"merge-base timed out for '{branch}': {exc} — "
                        f"skipping bump inference"
                    )
                else:
                    if merge_base_result.returncode != 0:
                        self._log(
                            f"merge-base failed for '{branch}': "
                            f"{merge_base_result.stderr.strip()} — skipping bump inference"
                        )
                    else:
                        merge_base_sha = merge_base_result.stdout.strip()
                        # Newly-merged path: use the live branch ref.
                        # already_merged path is handled by `continue` above.
                        branch_ref_for_bump = branch
                        try:
                            infer_result = infer_branch_bump(
                                self.project_root,
                                branch_ref_for_bump,
                                merge_base_sha,
                            )
                            if infer_result.bump is not None:
                                branch_bumps.append(infer_result.bump)
                                self._log(
                                    f"Inferred bump for '{branch}': {infer_result.bump.value}"
                                )
                            else:
                                self._log(
                                    f"Bump inference skipped for '{branch}': "
                                    f"{infer_result.reason}"
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
                self._populate_unattempted(report, branches)
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
                self._populate_unattempted(report, branches)
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
                self._populate_unattempted(report, branches)
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
                self._populate_unattempted(report, branches)
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
                self._populate_unattempted(report, branches)
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result in ("guardrail_repair_stalled", "guardrail_repair_exhausted"):
                iter_info = getattr(self, "_last_stall_iteration_count", None)
                iter_str = f" after {iter_info} iteration(s)" if iter_info else ""
                reason_word = "exhausted" if result == "guardrail_repair_exhausted" else "stalled"
                self._log(
                    f"Branch '{branch}' guardrail repair {reason_word}{iter_str} — "
                    f"escalated to human review"
                )
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — re-run after resolving"
                    )
                report.success = False
                report.pending_human = True
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = "guardrail_repair_stalled"
                report.version_aggregation_skipped = True
                self._populate_unattempted(report, branches)
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
                self._populate_unattempted(report, branches)
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
                self._populate_unattempted(report, branches)
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
                self._populate_unattempted(report, branches)
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
                self._populate_unattempted(report, branches)
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
                self._populate_unattempted(report, branches)
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
                self._populate_unattempted(report, branches)
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
                self._populate_unattempted(report, branches)
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
                self._populate_unattempted(report, branches)
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
                self._populate_unattempted(report, branches)
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
                self._populate_unattempted(report, branches)
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
                self._populate_unattempted(report, branches)
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result == "runtime_sync_collision":
                self._log(
                    f"Branch '{branch}' runtime sync collision — stopping merge sequence"
                )
                # The git merge succeeded; the merge commit is on HEAD.
                # Record the branch as merged so the report matches git state.
                # Task 13 / B1: post-condition still applies (branch must
                # be an ancestor); failure is logged but does not block
                # the runtime-sync-collision report.
                if branch not in report.merged_branches:
                    self._record_merged(
                        report, branch,
                        already_ancestor=False,
                        warnings_repaired=True,
                        pre_merge_sha="",
                    )
                self._log(
                    f"WARNING: Branch '{branch}' merge commit is on HEAD but "
                    f"runtime sync failed. On retry, include '{branch}' again "
                    f"so the already-merged path can complete runtime sync."
                )
                # Task 18 / B12: still infer this branch's bump and run
                # version aggregation — the merge commit is on HEAD, so
                # the version must move forward even though sync failed.
                if pre_merge_sha and pre_merge_version:
                    bump = self._infer_bump_for_branch(branch, pre_merge_sha)
                    if bump is not None:
                        branch_bumps.append(bump)
                        self._log(
                            f"Inferred bump for '{branch}': {bump.value} "
                            f"(despite runtime sync collision)"
                        )
                self._aggregate_versions(
                    report, branch_bumps, effective_pre_merge_version,
                )
                report.success = False
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = "runtime_sync_collision"
                self._populate_unattempted(report, branches)
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result == "runtime_sync_os_error":
                self._log(
                    f"Branch '{branch}' runtime sync OS error — stopping merge sequence"
                )
                if branch not in report.merged_branches:
                    self._record_merged(
                        report, branch,
                        already_ancestor=False,
                        warnings_repaired=True,
                        pre_merge_sha="",
                    )
                self._log(
                    f"WARNING: Branch '{branch}' merge commit is on HEAD but "
                    f"runtime sync failed. On retry, include '{branch}' again "
                    f"so the already-merged path can complete runtime sync."
                )
                # Task 18 / B12: still aggregate version for already-merged branches.
                if pre_merge_sha and pre_merge_version:
                    bump = self._infer_bump_for_branch(branch, pre_merge_sha)
                    if bump is not None:
                        branch_bumps.append(bump)
                self._aggregate_versions(
                    report, branch_bumps, effective_pre_merge_version,
                )
                report.success = False
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = "runtime_sync_os_error"
                self._populate_unattempted(report, branches)
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result == "runtime_sync_timeout":
                self._log(
                    f"Branch '{branch}' runtime sync timed out — stopping merge sequence"
                )
                if branch not in report.merged_branches:
                    self._record_merged(
                        report, branch,
                        already_ancestor=False,
                        warnings_repaired=True,
                        pre_merge_sha="",
                    )
                self._log(
                    f"WARNING: Branch '{branch}' merge commit is on HEAD but "
                    f"runtime sync failed. On retry, include '{branch}' again "
                    f"so the already-merged path can complete runtime sync."
                )
                # Task 18 / B12: still aggregate version for already-merged branches.
                if pre_merge_sha and pre_merge_version:
                    bump = self._infer_bump_for_branch(branch, pre_merge_sha)
                    if bump is not None:
                        branch_bumps.append(bump)
                self._aggregate_versions(
                    report, branch_bumps, effective_pre_merge_version,
                )
                report.success = False
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = "runtime_sync_timeout"
                self._populate_unattempted(report, branches)
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
                if not report.failure_reason:
                    report.failure_reason = "unexpected"
                report.version_aggregation_skipped = True
                self._populate_unattempted(report, branches)
                self._write_log()
                report.log_file = self.log_file
                return report

        # All branches merged successfully
        report.success = True
        self._log(f"All {len(branches)} branch(es) merged successfully")
        self._log(f"Merged: {', '.join(report.merged_branches)}")

        # SemVer aggregation: apply max bump to pyproject.toml and amend
        self._aggregate_versions(
            report, branch_bumps, effective_pre_merge_version,
        )

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

        self._populate_unattempted(report, branches)
        self._write_log()
        report.log_file = self.log_file
        return report

    def _is_head_published(self) -> bool:
        """Check whether the current HEAD has been pushed to any remote.

        Uses ``git for-each-ref --contains HEAD refs/remotes`` to detect
        remote-tracking branches that include the current commit. Returns
        ``True`` when at least one remote branch contains HEAD.
        """
        result = _run_git(
            self.project_root,
            "for-each-ref",
            "--format=%(refname)",
            "--contains", "HEAD",
            "refs/remotes",
            check=False,
            timeout=15,
        )
        if result.returncode != 0:
            return False
        return any(line.strip() for line in result.stdout.strip().split("\n") if line.strip())

    def _find_base_ref_for_already_merged(
        self, branch: str, head_sha: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Find the appropriate base ref and merged commit for an already-merged branch.

        Uses ``git rev-list --merges --ancestry-path`` to locate the merge
        commit(s) on the ancestry path from *branch* to *head_sha*, then
        filters to merge commits whose second parent has *branch* as an
        ancestor **and** whose first parent does **not** have *branch* as an
        ancestor. This distinguishes a merge *of* ``branch`` from a merge of
        some other branch that merely happens to lie on the ancestry path.

        Returns a tuple of:
        - ``base_ref``: the first parent of the newest matching merge commit,
          representing the state of HEAD immediately before ``branch`` was
          merged.
        - ``merged_commit``: the second parent of the newest matching merge
          commit, i.e. the actual commit from ``branch`` that was merged into
          HEAD. This is used instead of the live branch ref for version
          inference, because the branch tip may have advanced past the merge.

        Returns ``(None, None)`` when no matching merge commit is found (e.g.
        the branch was fast-forwarded, rebased, or squashed into HEAD) or when
        the git command fails.
        """
        # Find merge commits on the ancestry path from branch to head.
        # --ancestry-path selects commits that are on a path from branch to
        # head (descendants of branch and ancestors of head).
        result = _run_git(
            self.project_root,
            "rev-list",
            "--merges",
            "--ancestry-path",
            f"{branch}..{head_sha}",
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            self._log(
                f"_find_base_ref_for_already_merged: rev-list failed for "
                f"branch '{branch}': {result.stderr.strip()}"
            )
            return None, None

        merge_commits = [
            line.strip() for line in result.stdout.strip().split("\n") if line.strip()
        ]
        if not merge_commits:
            # No merge commit on the ancestry path — the branch may have been
            # fast-forwarded, rebased, or squashed. Cannot determine pre-merge
            # state reliably.
            self._log(
                f"_find_base_ref_for_already_merged: no merge commit found for "
                f"'{branch}' on ancestry path from {head_sha[:8]} — "
                f"branch may have been fast-forwarded or rebased; "
                f"bump inference skipped"
            )
            return None, None

        # Filter to merge commits that actually merged this branch:
        #   - branch must be an ancestor of the second parent (theirs)
        #   - branch must NOT be an ancestor of the first parent (ours)
        # This prevents mis-identifying a later merge of another branch
        # (whose first parent already contains this branch's changes) as
        # the merge of this branch.
        branch_merge_commits: list[str] = []
        for merge_commit in merge_commits:
            merge_theirs = f"{merge_commit}^2"
            # Verify this merge commit actually merged THIS branch:
            # merge_commit^2 (the exact commit merged) must be an ancestor of
            # the named branch ref. This distinguishes a direct merge of
            # ``branch`` from a later merge of an unrelated branch that merely
            # happens to have ``branch`` in its ancestry.
            is_ancestor_of_branch = _run_git(
                self.project_root,
                "merge-base",
                "--is-ancestor",
                merge_theirs,
                branch,
                check=False,
                timeout=15,
            )
            if is_ancestor_of_branch.returncode != 0:
                # merge_commit^2 is not reachable from branch — this merge
                # is of some other branch, not this one.
                continue
            is_ancestor_of_first = _run_git(
                self.project_root,
                "merge-base",
                "--is-ancestor",
                merge_theirs,
                f"{merge_commit}^1",
                check=False,
                timeout=15,
            )
            if is_ancestor_of_first.returncode == 0:
                # merge_commit^2 is already reachable from merge_commit^1.
                # This means the merge is a redundant / no-op --no-ff re-merge
                # of an already-integrated branch, not the original merge of
                # this branch. Skip it.
                continue
            branch_merge_commits.append(merge_commit)

        if not branch_merge_commits:
            self._log(
                f"_find_base_ref_for_already_merged: no merge commit of "
                f"'{branch}' found on ancestry path from {head_sha[:8]} — "
                f"branch may have been fast-forwarded or rebased; "
                f"bump inference skipped"
            )
            return None, None

        # branch_merge_commits are returned newest-first; use the newest merge
        # commit (most recent time the branch was merged). This handles the
        # case where a branch was merged, reverted, and re-merged — the oldest
        # merge's parent would represent the original integration's pre-state,
        # which may not reflect the true state before the re-merge.
        newest_merge = branch_merge_commits[0]

        # Warn for octopus merges (more than 2 parents). The first parent is
        # the pre-merge HEAD, which is the correct base for our purposes, but
        # other merged branches' changes are also in the ancestry.
        parents_result = _run_git(
            self.project_root,
            "rev-parse",
            f"{newest_merge}^@",
            check=False,
            timeout=15,
        )
        if parents_result.returncode == 0:
            parents = [
                line.strip()
                for line in parents_result.stdout.strip().split("\n")
                if line.strip()
            ]
            if len(parents) > 2:
                self._log(
                    f"WARNING: _find_base_ref_for_already_merged: merge commit "
                    f"{newest_merge[:8]} for '{branch}' is an octopus merge "
                    f"({len(parents)} parents). Using first parent as base; "
                    f"other branches merged in the same octopus commit may "
                    f"affect version inference."
                )

        parent_result = _run_git(
            self.project_root,
            "rev-parse",
            f"{newest_merge}^",
            check=False,
            timeout=15,
        )
        if parent_result.returncode != 0:
            self._log(
                f"_find_base_ref_for_already_merged: could not get first parent "
                f"of merge commit {newest_merge[:8]} for '{branch}'"
            )
            return None, None

        base_ref = parent_result.stdout.strip()

        # Get the second parent (the actual commit from the branch that was merged)
        theirs_result = _run_git(
            self.project_root,
            "rev-parse",
            f"{newest_merge}^2",
            check=False,
            timeout=15,
        )
        merged_commit = (
            theirs_result.stdout.strip()
            if theirs_result.returncode == 0
            else None
        )

        return base_ref, merged_commit

    def _sync_runtime(self, branch: str, report: MergeReport) -> Optional[str]:
        """Sync runtime data from *branch*'s bound worktree into current se3/.

        Returns ``None`` on success, when the source worktree is missing,
        or when collisions are bypassed in lenient mode.
        Returns ``"runtime_sync_collision"`` when a tier A file collides
        and ``strict_runtime_sync`` is ``True``. In lenient mode,
        collisions (including directory collisions) are bypassed via
        sidecar files or recorded as skipped rather than halting.
        Returns ``"runtime_sync_os_error"`` when an unrecoverable OS error
        occurs during the sync.  In lenient mode, transient errors such as
        disk full or permission denied are absorbed as ``skipped_files``
        entries rather than reaching this return value.  An unexpected
        OSError that escapes ``sync_branch_runtime`` is logged and treated
        as a skipped branch (the merge sequence continues); this path only
        halts the sequence when ``strict_runtime_sync`` is ``True``.
        Returns ``"runtime_sync_timeout"`` when the sync operation times out.
        """
        try:
            sync_report = sync_branch_runtime(
                self.project_root, branch,
                strict=self.strict_runtime_sync,
            )
            if sync_report.skipped:
                self._log(f"Runtime sync skipped for '{branch}': no bound worktree")
                report.runtime_sync_skipped_branches.append(branch)
            else:
                if sync_report.copied:
                    self._log(
                        f"Runtime sync copied for '{branch}': {sync_report.copied}"
                    )
                if sync_report.discarded:
                    self._log(
                        f"Runtime sync discarded for '{branch}': "
                        f"{len(sync_report.discarded)} file(s)"
                    )
                    report.runtime_sync_discarded.append(
                        (branch, sync_report.discarded)
                    )
                if sync_report.skipped_files:
                    self._log(
                        f"Runtime sync skipped files for '{branch}': "
                        f"{sync_report.skipped_files}"
                    )
                    report.runtime_sync_skipped_files.append(
                        (branch, sync_report.skipped_files)
                    )
                if sync_report.collisions:
                    for collision in sync_report.collisions:
                        marker = "[written]" if collision.written else "[audit-only]"
                        dest_hash_render = (
                            collision.dest_hash
                            if collision.dest_hash == DEST_HASH_UNAVAILABLE
                            else f"{collision.dest_hash[:8]}.."
                        )
                        self._log(
                            f"Runtime sync collision {marker} for '{branch}': "
                            f"{collision.original_rel_path} -> "
                            f"{collision.sidecar_rel_path} "
                            f"(src_hash={collision.src_hash[:8]}.. "
                            f"dest_hash={dest_hash_render})"
                        )
                        report.runtime_sync_collisions.append(collision)
                if sync_report.idempotent_bypasses:
                    self._log(
                        f"Runtime sync idempotent bypasses for '{branch}': "
                        f"{sync_report.idempotent_bypasses} sidecar file(s) "
                        f"already matched source content (possible stale "
                        f"sidecar leftovers from prior aborted runs)"
                    )
                    report.runtime_sync_idempotent_bypasses.append(
                        (branch, sync_report.idempotent_bypasses)
                    )
                    # Carry the per-file audit detail forward so callers
                    # investigating a stale-sidecar warning have the exact
                    # sidecar paths without rerunning under DEBUG logging.
                    report.runtime_sync_idempotent_records.extend(
                        sync_report.idempotent_bypass_records
                    )
        except RuntimeSyncCollision as exc:
            self._log(f"Runtime sync collision for '{branch}': {exc}")
            report.failure_reason = "runtime_sync_collision"
            report.runtime_sync_collision_path = exc.rel_path
            return "runtime_sync_collision"
        except OSError as exc:
            self._log(
                f"Runtime sync OS error for '{branch}': {exc}"
            )
            if self.strict_runtime_sync:
                report.failure_reason = "runtime_sync_os_error"
                return "runtime_sync_os_error"
            # In lenient mode, unexpected OSErrors are logged but do not halt
            # the merge sequence — individual file-level errors are already
            # absorbed as skipped_files inside sync_branch_runtime.
            report.runtime_sync_skipped_branches.append(branch)
            return None
        # Defensive catch: sync_branch_runtime itself uses no subprocess
        # timeouts, but _get_worktree_path_for_branch (called inside it)
        # may raise TimeoutExpired from its internal git invocation.
        except subprocess.TimeoutExpired as exc:
            self._log(
                f"Runtime sync timeout for '{branch}': {exc}"
            )
            report.failure_reason = "runtime_sync_timeout"
            return "runtime_sync_timeout"
        return None

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
                sync_result = self._sync_runtime(branch, report)
                if sync_result:
                    return sync_result
                return "already_merged"

            try:
                guardrails_result = self._run_guardrails(
                    pre_merge_sha, post_merge_sha, branch, strategy=self.strategy,
                )
            except GuardrailRepairStalled as exc:
                self._log(
                    f"Guardrail repair stalled for '{branch}' after "
                    f"{exc.iteration_count} iteration(s) — escalated to human review"
                )
                # Task 18 / B11: only set human_call_file when the call
                # file actually exists. Setting it to ``None`` would
                # cause downstream renderers to display "Call file: None".
                if exc.call_file is not None:
                    report.human_call_file = exc.call_file
                report.failure_reason = exc.failure_reason
                self._last_stall_iteration_count = exc.iteration_count
                return exc.failure_reason
            except GuardrailRepairFailed as exc:
                if exc.failure_reason == "guardrail_check_failed":
                    self._log(
                        f"Guardrails check itself crashed for '{branch}' in fast mode: {exc}"
                    )
                elif exc.failure_reason == "guardrail_repair_stalled_call_failed":
                    self._log(
                        f"Guardrail repair stalled for '{branch}' in fast mode: "
                        f"rollback succeeded but the stalled human call file could not be written. {exc}"
                    )
                elif exc.failure_reason == "guardrail_repair_exhausted_call_failed":
                    self._log(
                        f"Guardrail repair exhausted for '{branch}' in fast mode: "
                        f"rollback succeeded but the exhausted human call file could not be written. {exc}"
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
            pre_guardrail_sha = post_merge_sha
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
                # Cannot verify whether HEAD changed; be lenient and skip
                # the merge-commit post-condition (the branch-merged check
                # still runs).
                self._last_merge_repair_changed_head = True
            else:
                if pre_guardrail_sha and post_merge_sha and pre_guardrail_sha != post_merge_sha:
                    self._last_merge_repair_changed_head = True
                    self._log(
                        f"Guardrail repair changed HEAD ({pre_guardrail_sha[:8]} -> "
                        f"{post_merge_sha[:8]}); skipping merge-commit post-condition"
                    )
            sync_result = self._sync_runtime(branch, report)
            if sync_result:
                return sync_result
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
        except GuardrailRepairStalled as exc:
            self._log(
                f"Guardrail repair stalled for '{branch}' after "
                f"{exc.iteration_count} iteration(s) — escalated to human review"
            )
            # Task 18 / B11: only set human_call_file when the call file
            # actually exists.
            if exc.call_file is not None:
                report.human_call_file = exc.call_file
            report.failure_reason = exc.failure_reason
            self._last_stall_iteration_count = exc.iteration_count
            return exc.failure_reason
        except GuardrailRepairFailed as exc:
            if exc.failure_reason == "guardrail_check_failed":
                self._log(
                    f"Guardrails check itself crashed for '{branch}' in fast mode: {exc}"
                )
            elif exc.failure_reason == "guardrail_repair_stalled_call_failed":
                self._log(
                    f"Guardrail repair stalled for '{branch}' in fast mode: "
                    f"rollback succeeded but the stalled human call file could not be written. {exc}"
                )
            elif exc.failure_reason == "guardrail_repair_exhausted_call_failed":
                self._log(
                    f"Guardrail repair exhausted for '{branch}' in fast mode: "
                    f"rollback succeeded but the exhausted human call file could not be written. {exc}"
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
        pre_guardrail_sha = post_merge_sha
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
            # Cannot verify whether HEAD changed; be lenient and skip the
            # merge-commit post-condition (the branch-merged check still runs).
            self._last_merge_repair_changed_head = True
        else:
            if pre_guardrail_sha and post_merge_sha and pre_guardrail_sha != post_merge_sha:
                self._last_merge_repair_changed_head = True
                self._log(
                    f"Guardrail repair changed HEAD ({pre_guardrail_sha[:8]} -> "
                    f"{post_merge_sha[:8]}); skipping merge-commit post-condition"
                )
        sha_note = "" if sha_fresh else " (SHA may be stale)"
        self._log(
            f"LLM-resolved merge of '{branch}' committed successfully "
            f"(SHA: {post_merge_sha}){sha_note}"
        )
        sync_result = self._sync_runtime(branch, report)
        if sync_result:
            return sync_result
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
                    f"Fast mode aborts without rollback or human call when SHAs "
                    f"are missing — the merge commit may still be in HEAD.",
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

            # --- fast strategy: attempt LLM repair with iteration limit ---
            if strategy == MergeStrategy.FAST:
                # Use a local variable for the working violation set so we
                # never mutate the original gr_report object.
                current_violations = gr_report.violations

                self._log(
                    f"Fast strategy: attempting LLM repair of "
                    f"{len(current_violations)} guardrail violation(s) "
                    f"(max {self._max_repair_iterations} iterations)"
                )

                # Track the previous violation-set hash to detect stalls.
                # A stall requires the same hash in *two consecutive repair
                # iterations*, matching the spec's "连续 2 轮 hash 相同".
                # Only the immediately previous hash is compared (not a set of
                # all prior hashes) so that oscillating patterns which happen
                # to revisit an earlier state after making progress are not
                # falsely classified as stalled.
                # Task 10 / A9 fix: use None instead of "" so the first
                # iteration's hash is never spuriously compared against an
                # empty string.
                last_hash: Optional[str] = None

                # Gather original and merged spec contents.
                # original_specs is read once (pre_sha never changes).
                # merged_specs is refreshed each iteration from the current HEAD
                # so that the LLM sees the latest state after any amendments by
                # previous repair rounds.
                spec_files = _get_changed_spec_files(
                    self.project_root, pre_sha, post_sha,
                )
                original_specs: dict[str, str] = {}
                for sp in spec_files:
                    orig = _read_file_from_ref(self.project_root, sp, pre_sha)
                    if orig is None:
                        self._log(
                            f"WARNING: Could not read original content of {sp} "
                            f"from ref {pre_sha} — including placeholder "
                            f"in repair prompt"
                        )
                        orig = f"[Content unavailable at ref {pre_sha}]"
                    original_specs[sp] = orig

                for iteration in range(1, self._max_repair_iterations + 1):
                    # Refresh merged specs from current HEAD so the LLM sees
                    # the latest state after any amendments from previous repair
                    # rounds. Falls back to post_sha if HEAD cannot be read.
                    try:
                        head_result = _run_git(
                            self.project_root, "rev-parse", "HEAD",
                            check=False, timeout=15,
                        )
                        current_head = (
                            head_result.stdout.strip()
                            if head_result.returncode == 0 else ""
                        )
                    except subprocess.TimeoutExpired:
                        current_head = ""
                    read_ref = current_head if current_head else post_sha

                    merged_specs: dict[str, str] = {}
                    for sp in spec_files:
                        merged = _read_file_from_ref(
                            self.project_root, sp, read_ref,
                        )
                        if merged is None:
                            # Fallback to original post_sha if HEAD ref read fails
                            merged = _read_file_from_ref(
                                self.project_root, sp, post_sha,
                            )
                            if merged is None:
                                self._log(
                                    f"WARNING: Could not read merged content of "
                                    f"{sp} from ref {post_sha} — including "
                                    f"placeholder in repair prompt"
                                )
                                merged = f"[Content unavailable at ref {post_sha}]"
                        merged_specs[sp] = merged

                    self._log(
                        f"Fast strategy: repair iteration {iteration}/"
                        f"{self._max_repair_iterations}"
                    )

                    repair_result = self._repairer.repair_violations(
                        branch=branch,
                        pre_sha=pre_sha,
                        post_sha=post_sha,
                        violations=current_violations,
                        original_spec_contents=original_specs,
                        merged_spec_contents=merged_specs,
                    )

                    if repair_result.success:
                        self._log(
                            f"Guardrail repair succeeded for '{branch}' at "
                            f"iteration {iteration}: "
                            f"{len(repair_result.repaired_files)} file(s) corrected"
                        )
                        # Task 11 / A11 fix: after repair success, verify
                        # the branch is actually merged (ancestry check).
                        # This catches the case where the merge commit was
                        # silently lost during the repair process.
                        try:
                            assert_branch_merged(self.project_root, branch)
                        except PostConditionViolated as pcv:
                            self._log(
                                f"Post-condition failed after guardrail repair: "
                                f"{pcv.detail}"
                            )
                            raise GuardrailRepairFailed(
                                f"Guardrail repair succeeded but post-condition failed: "
                                f"{pcv.detail}",
                                failure_reason="postcond_branch_not_merged",
                            ) from pcv
                        return None

                    # Repair failed — re-run guardrails to get fresh violations
                    self._log(
                        f"Guardrail repair iteration {iteration} failed: "
                        f"{repair_result.error}"
                    )

                    try:
                        fresh_report = self._guardrails.check_merge_result(
                            pre_sha, post_sha,
                        )
                    except Exception as exc:
                        self._log(
                            f"Guardrails re-check failed after repair: {exc}"
                        )
                        try:
                            self._rollback_to(pre_sha)
                        except (RuntimeError, subprocess.TimeoutExpired) as rbe:
                            raise GuardrailRepairFailed(
                                f"Guardrail repair failed at iteration {iteration} "
                                f"and re-check crashed. Rollback also failed: {rbe}",
                                failure_reason="guardrail_check_failed",
                                rollback_failed=True,
                            ) from rbe
                        raise GuardrailRepairFailed(
                            f"Guardrail repair failed at iteration {iteration} "
                            f"and re-check crashed: {exc}",
                            failure_reason="guardrail_check_failed",
                        ) from exc

                    if fresh_report.passed:
                        # Verify HEAD has not drifted from post_sha before
                        # accepting the side-effect clearance. If the repairer
                        # left an amended commit, post_sha is stale and we
                        # must refresh it for downstream callers.
                        try:
                            head_sha = _run_git(
                                self.project_root, "rev-parse", "HEAD",
                                check=False, timeout=15,
                            ).stdout.strip()
                        except subprocess.TimeoutExpired:
                            self._log(
                                "WARNING: git rev-parse HEAD timed out during "
                                f"side-effect clearance check at iteration {iteration}. "
                                "HEAD drift verification was skipped; downstream "
                                "SHA may be stale."
                            )
                            head_sha = ""
                        if head_sha and head_sha != post_sha:
                            self._log(
                                f"Guardrails passed on re-check after iteration "
                                f"{iteration} — repair reported failure but "
                                f"violations were cleared by side-effect; "
                                f"HEAD moved from {post_sha[:8]} to "
                                f"{head_sha[:8]}"
                            )
                            # Caller will refresh post_merge_sha when it sees
                            # the merged return path.
                        else:
                            self._log(
                                f"Guardrails passed on re-check after iteration "
                                f"{iteration} — repair reported failure but "
                                f"violations were cleared by side-effect; "
                                f"accepting result"
                            )
                        return None

                    current_hash = violation_set_hash(fresh_report.violations)
                    self._log(
                        f"Repair iteration {iteration}: violation hash "
                        f"{current_hash[:8]}... "
                        f"({len(fresh_report.violations)} violation(s))"
                    )

                    if last_hash is not None and current_hash == last_hash:
                        # Stalled — violation set unchanged from previous
                        # repair iteration (consecutive identical hash).
                        self._log(
                            f"Guardrail repair stalled at iteration {iteration}: "
                            f"violation set hash {current_hash[:8]}... unchanged from previous iteration"
                        )
                        rollback_exc = None
                        try:
                            self._rollback_to(pre_sha)
                        except (RuntimeError, subprocess.TimeoutExpired) as rbe:
                            self._log(
                                f"Rollback failed after stalled guardrail repair at "
                                f"iteration {iteration}: {rbe}"
                            )
                            rollback_exc = rbe

                        violation_dicts = self._violations_to_dicts(
                            fresh_report.violations,
                            branch=branch,
                        )
                        call_file: Optional[Path] = None
                        try:
                            call_file = self._human_writer.write_guardrail_call(
                                branch=branch,
                                violations=violation_dicts,
                                pre_merge_sha=pre_sha,
                                call_type="guardrail_repair_stalled",
                                iteration_count=iteration,
                            )
                        except Exception as exc:
                            self._log(
                                f"Failed to write stalled guardrail call file: {exc}"
                            )
                            if rollback_exc is None:
                                raise GuardrailRepairFailed(
                                    f"Guardrail repair stalled at iteration {iteration} "
                                    f"and call file could not be written: {exc}",
                                    failure_reason="guardrail_repair_stalled_call_failed",
                                ) from exc
                            raise GuardrailRollbackError(
                                f"Guardrail repair stalled at iteration {iteration}. "
                                f"Rollback failed: {rollback_exc}. "
                                f"Additionally, the human call file could not be written: {exc}. "
                                f"Working tree may be in an inconsistent state. "
                                f"Manual intervention required.",
                                call_file=None,
                            ) from exc

                        try:
                            self._human_writer.print_instructions(call_file)
                        except Exception as exc:
                            self._log(
                                f"WARNING: Failed to print instructions (call file was written "
                                f"successfully): {exc}"
                            )

                        if rollback_exc is not None:
                            raise GuardrailRollbackError(
                                f"Guardrail repair stalled at iteration {iteration} "
                                f"but rollback failed. The human call file was written at "
                                f"{call_file} for diagnostic evidence.",
                                call_file=call_file,
                            ) from rollback_exc

                        raise GuardrailRepairStalled(
                            f"Guardrail repair stalled after {iteration} "
                            f"iteration(s): LLM could not reduce violations",
                            call_file=call_file,
                            iteration_count=iteration,
                            last_violation_hash=current_hash,
                            failure_reason="guardrail_repair_stalled",
                        )

                    last_hash = current_hash
                    # Update working violation list for next iteration's repair
                    # prompt. Use a local variable rather than mutating the
                    # original gr_report object so any retained references
                    # elsewhere do not observe stale data.
                    current_violations = fresh_report.violations
                else:
                    # The else clause runs only when the loop completes all
                    # iterations without an early return (repair success,
                    # side-effect clearance, or stall exception).
                    # Max iterations reached — violations persist but hash keeps
                    # changing. Escalate to human call consistently with the stall
                    # path instead of aborting outright.
                    self._log(
                        f"Guardrail repair exhausted after {self._max_repair_iterations} "
                        f"iterations — escalating to human review"
                    )
                    rollback_exc = None
                    try:
                        self._rollback_to(pre_sha)
                    except (RuntimeError, subprocess.TimeoutExpired) as rbe:
                        self._log(
                            f"Rollback failed after exhausted guardrail repair: {rbe}"
                        )
                        rollback_exc = rbe

                    violation_dicts = self._violations_to_dicts(
                        current_violations,
                        branch=branch,
                    )
                    call_file: Optional[Path] = None
                    try:
                        call_file = self._human_writer.write_guardrail_call(
                            branch=branch,
                            violations=violation_dicts,
                            pre_merge_sha=pre_sha,
                            call_type="guardrail_repair_exhausted",
                            iteration_count=iteration,
                        )
                    except Exception as exc:
                        self._log(
                            f"Failed to write exhausted guardrail call file: {exc}"
                        )
                        if rollback_exc is None:
                            raise GuardrailRepairFailed(
                                f"Guardrail repair exhausted after {self._max_repair_iterations} "
                                f"iterations and call file could not be written: {exc}",
                                failure_reason="guardrail_repair_exhausted_call_failed",
                            ) from exc
                        raise GuardrailRollbackError(
                            f"Guardrail repair exhausted after {self._max_repair_iterations} "
                            f"iterations. Rollback failed: {rollback_exc}. "
                            f"Additionally, the human call file could not be written: {exc}. "
                            f"Working tree may be in an inconsistent state. "
                            f"Manual intervention required.",
                            call_file=None,
                        ) from exc

                    try:
                        self._human_writer.print_instructions(call_file)
                    except Exception as exc:
                        self._log(
                            f"WARNING: Failed to print instructions (call file was written "
                            f"successfully): {exc}"
                        )

                    if rollback_exc is not None:
                        raise GuardrailRollbackError(
                            f"Guardrail repair exhausted after {self._max_repair_iterations} "
                            f"iterations but rollback failed. The human call file "
                            f"was written at {call_file} for diagnostic evidence.",
                            call_file=call_file,
                        ) from rollback_exc

                    raise GuardrailRepairExhausted(
                        f"Guardrail repair exhausted after {self._max_repair_iterations} "
                        f"iteration(s): LLM could not reduce violations",
                        call_file=call_file,
                        iteration_count=iteration,
                        last_violation_hash=current_hash,
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
                violation_dicts = self._violations_to_dicts(
                    gr_report.violations,
                    branch=branch,
                )
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
                violation_dicts = self._violations_to_dicts(
                    gr_report.violations,
                    branch=branch,
                )
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
        except GuardrailRepairStalled:
            raise  # re-raise stalled escalations without wrapping
        except GuardrailRepairFailed:
            raise  # re-raise fast-mode repair failures without wrapping
        except GuardrailRollbackError:
            raise  # re-raise so caller surfaces call_file in rollback_failed path
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
    def _violations_to_dicts(violations: list, branch: str = "") -> list[dict]:
        """Convert a list of GuardrailViolation objects to plain dicts.

        Centralised so that rollback-failure and rollback-success paths in
        ``_run_guardrails`` stay consistent when the data model changes.

        Args:
            violations: List of GuardrailViolation objects.
            branch: Optional branch name. When provided, ``branch_name`` and
                ``trigger_branch`` are injected into each violation's evidence
                dict so the human call file shows which branch produced the
                violation.
        """
        result = []
        for v in violations:
            d = {
                "file_path": v.file_path,
                "violation_type": v.violation_type,
                "message": v.message,
            }
            if getattr(v, "evidence", None) is not None:
                # Shallow copy: evidence dicts are consumed once (written to
                # JSON) and then discarded. Explicitly copy known mutable
                # fields so downstream mutation cannot affect the original.
                ev = dict(v.evidence)
                if "when_clauses" in ev and isinstance(ev["when_clauses"], list):
                    ev["when_clauses"] = list(ev["when_clauses"])
            else:
                ev = {}
            if branch:
                ev["branch_name"] = branch
                ev["trigger_branch"] = branch
                ev["branch_kind"] = "merge"
            if ev:
                d["evidence"] = ev
            result.append(d)
        return result

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
