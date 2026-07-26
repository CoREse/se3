"""MergeOrchestrator — Sequential merge of branches into current branch.

Orchestrates the merge flow: for each branch, call git merge, handle
clean merge / conflict / non-conflict-failure, run guardrails, and
aggregate results.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

from ...i18n import t
from ..version_bumper import BumpType, Version
from ..worktree import _run_git, get_conflicting_files, get_current_branch
from .cleanup import CleanupManager, CleanupReport
from .conflict_context import build as build_conflict_context
from .conflict_resolver import (
    BatchContext,
    ConflictResolver,
    LLMResolution,
    MergeStrategy,
    _load_max_conflict_resolve_iterations,
)
from .deterministic_resolvers import (
    DeterministicOutcome,
    _find_resolver,
    resolve_deterministic,
)
from ...commands.merge.secret_redact import redact_text
from .guardrail_repair import GuardrailRepairer, GuardrailRepairInconsistentState
from .guardrails import (
    MergeGuardrailsCheck,
    _get_changed_spec_files,
    _read_file_from_ref,
    violation_set_hash,
)
from .human_call import HumanCallWriter, _generate_call_filename
from .issue_renumber import (
    advance_next_id_to_max,
    append_description_note,
    format_ambiguous_reference_note,
    format_renumber_trace,
    live_reference_count,
    reserve_next_id,
    rewrite_issue_references,
    rewrite_references_in_added_lines,
)
from ...commands.merge.failure_reason import FailureReason
from ...commands.merge.postcondition import (
    PostConditionViolated,
    assert_branch_merged,
    assert_head_is_merge_commit,
    assert_version_bumped,
)
from .runtime_sync import (
    DEST_HASH_UNAVAILABLE,
    BypassedCollision,
    IssueMergeRecord,
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
from ...commands.merge.result_model import MergeOutcome, MergeReport

logger = logging.getLogger(__name__)

# The structured return type of the library entry points (:func:`integrate`).
# The design demotes this orchestrator from a top-level entry that manages its
# own flow-control to a library that hands a structured result back to the
# caller. ``MergeResult`` is the typed :class:`MergeReport`; the alias names the
# library contract without forking a second nearly-identical dataclass.
MergeResult = MergeReport

# POSIX relative-path prefixes of the git-tracked data SE3 delegates to a
# running session to mutate in the MAIN working tree between commit steps.
# Two classes of such committed data exist and BOTH must be here, or a merge
# git could complete is blocked with a spurious dirty_working_tree:
#   * se3/issues/ — issue open/close writes files under it and bumps
#     se3/issues/.next_id (divergence routed to NextIdResolver);
#   * se3/code-index.md — flow steps rewrite it incrementally, so it is
#     routinely dirty between commit steps (the repo is in exactly this state
#     right now); its divergence has its own deterministic code-index resolver
#     (#280, same class as NextIdResolver).
# When any of these are dirty at merge start git refuses to begin the merge
# ("Your local changes would be overwritten by merge"), so the pre-flight
# auto-commits them into a sync commit and lets the three-way merge + the
# matching deterministic resolver take over. The whitelist is a CLOSED list
# split by entry kind so each kind's match semantics are explicit at the
# definition site (extending it to other tier-A delegated data is a one-line
# change to the matching tuple):
#   * _SELF_MANAGED_DIRTY_PREFIXES — directory-prefix entries (trailing slash),
#     matched by path prefix so a match is confined to paths INSIDE the dir
#     (never a sibling like se3/issuesX);
#   * _SELF_MANAGED_DIRTY_FILES — exact-file entries, matched by full-path
#     equality ONLY — NOT a prefix, so a tracked sibling like
#     se3/code-index.md.bak / .orig is treated as an outside file, not silently
#     swallowed by the whitelist.
_SELF_MANAGED_DIRTY_PREFIXES: tuple[str, ...] = ("se3/issues/",)
_SELF_MANAGED_DIRTY_FILES: tuple[str, ...] = ("se3/code-index.md",)


def _is_self_managed_dirty_path(path: str) -> bool:
    """True if *path* (a POSIX repo-relative path) is SE3 self-managed.

    A :data:`_SELF_MANAGED_DIRTY_PREFIXES` entry is a directory prefix and
    matches any path strictly inside it; a :data:`_SELF_MANAGED_DIRTY_FILES`
    entry is a FILE and matches only that exact path. Using exact equality for
    file entries (rather than ``str.startswith``) is what keeps siblings such
    as ``se3/code-index.md.bak`` OUT of the whitelist — otherwise their dirty
    state would be misclassified as self-managed while the sync commit's
    pathspec never actually stages them.
    """
    if any(path.startswith(pref) for pref in _SELF_MANAGED_DIRTY_PREFIXES):
        return True
    return path in _SELF_MANAGED_DIRTY_FILES


def _atomic_write_text(path: Path, content: str) -> None:
    """Write *content* to *path* atomically.

    Uses a temp file in the same directory + ``os.replace`` so the
    file is either fully old or fully new — never partial.  An fsync
    on the temp file flushes the bytes before rename so a crash
    between rename and shutdown can't lose the write.

    Preserves the original file's permission mode when replacing an
    existing file (``tempfile.mkstemp`` creates at mode 0600 by
    default, which would silently make world-readable files private).

    Durability contract matches
    :func:`tianluo.engine.merge.version_aggregator._atomic_write_text`:
    after the rename completes the parent directory is fsync'd so the
    rename metadata survives a crash.  Without this, a power loss
    between os.replace and the kernel's next implicit dir flush could
    undo the rename and leave the original content in place even
    though the temp file's bytes were durably written.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    # Capture the original file mode so we can restore it after rename.
    original_mode: Optional[int] = None
    if path.exists():
        try:
            original_mode = path.stat().st_mode
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(dir=str(parent), suffix=".tmp")
    # Wrap fdopen in its own try/except so that an early failure (e.g.
    # os.fdopen raising before the with-block is entered) does not
    # leak the raw fd from mkstemp.  Once fdopen succeeds, the with
    # statement owns the fd's lifetime and closing it here would be a
    # double-close.  The narrow except catches OSError (the realistic
    # ENOMEM/ENFILE/EMFILE family) plus MemoryError (interpreter-level
    # allocation failure) without falling into a bare-Exception swallow
    # that would mask logic bugs in this code path.
    try:
        f = os.fdopen(fd, "w", encoding="utf-8")
    except (OSError, MemoryError):
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        with f:
            f.write(content)
            f.flush()
            if original_mode is not None:
                try:
                    os.fchmod(f.fileno(), original_mode)
                except OSError:
                    pass
            try:
                os.fsync(f.fileno())
            except OSError:
                # Some filesystems (e.g. tmpfs) reject fsync on a regular
                # file.  Log at debug and continue — durability is reduced
                # but the data has been written.
                logger.debug(
                    "fsync not supported for temp file %s; skipping flush",
                    tmp,
                )
        os.replace(tmp, str(path))
        # Durability: fsync the parent directory so the rename metadata is
        # flushed.  A crash between os.replace and the next implicit dir
        # flush could undo the rename, leaving the file at its original
        # content even though the temp file was durably written.
        try:
            dir_fd = os.open(str(parent), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            # Some filesystems (tmpfs, certain FUSE mounts) reject
            # directory fsync.  Log at debug for observability without
            # failing the write.
            logger.debug(
                "Parent-directory fsync failed for %s: %s "
                "(rename durability reduced)",
                parent, exc,
            )
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# Default max repair iterations when no project config overrides it.
# Stall is detected when two *consecutive repair-iteration* hashes match.
# The initial gr_report hash is compared against last_hash so that even
# with max_iterations=1 a no-op repair (iter-1 hash == initial hash) is
# detected as a stall and escalated to human call.
_DEFAULT_MAX_REPAIR_ITERATIONS = 2
# Upper bound for user-configured max_iterations.  Values above this are
# capped to prevent runaway LLM loops.  The cap is a safety guardrail,
# not a tunable parameter — it is intentionally not exposed in se3.yaml.
_MAX_REPAIR_ITERATIONS_HARD_CAP = 20

if _DEFAULT_MAX_REPAIR_ITERATIONS < 1:
    raise ValueError(
        "_DEFAULT_MAX_REPAIR_ITERATIONS must be >= 1 so the repair loop executes at least "
        "once and the exhausted-path fallback is well-defined."
    )


def _load_max_repair_iterations(project_root: Path) -> int:
    """Read max repair iterations from se3.yaml, with safe fallback.

    Looks under ``merge.guardrail_repair.max_iterations``.
    Invalid or missing values fall back to the default.

    Catches a narrow set of expected failure modes (import failures, I/O
    errors from a stat-able-but-unreadable config, malformed YAML, or
    unexpected return type from the loader) rather than a bare
    ``except Exception``: this preserves the project's "no silent
    except-Exception" rule while still tolerating the realistic config
    failures.  Catching every exception would mask programmer errors in
    the loader (yaml schema migrations, refactors of the loader's
    return shape, etc.).
    """
    from ...config import load_project_yaml

    try:
        data, _src = load_project_yaml(project_root)
    except (ImportError, OSError, ValueError, TypeError, AttributeError) as exc:
        logger.warning(
            "Failed to load project config for max_repair_iterations: %s "
            "— using default %d",
            exc, _DEFAULT_MAX_REPAIR_ITERATIONS,
        )
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
            "merge.guardrail_repair.max_iterations=%d is below 1; "
            "using default %d",
            val, _DEFAULT_MAX_REPAIR_ITERATIONS,
        )
        return _DEFAULT_MAX_REPAIR_ITERATIONS
    if val > _MAX_REPAIR_ITERATIONS_HARD_CAP:
        logger.warning(
            "merge.guardrail_repair.max_iterations=%d exceeds the hard cap "
            "of %d (safety guardrail to prevent runaway LLM loops); "
            "capping at %d.  To raise the cap, modify "
            "_MAX_REPAIR_ITERATIONS_HARD_CAP in orchestrator.py — it is "
            "intentionally NOT exposed in se3.yaml.",
            val, _MAX_REPAIR_ITERATIONS_HARD_CAP, _MAX_REPAIR_ITERATIONS_HARD_CAP,
        )
        return _MAX_REPAIR_ITERATIONS_HARD_CAP
    return val


# Default git merge timeout (seconds).  60s covers most repos but may be
# too tight for very large repos, cold filesystems, or git-LFS smudge.
_DEFAULT_GIT_MERGE_TIMEOUT = 60


def _load_git_merge_timeout(project_root: Path) -> int:
    """Read git merge timeout from se3.yaml, with safe fallback.

    Looks under ``merge.git_merge_timeout``.  Invalid or missing values
    fall back to the default.

    Catches a narrow set of expected failure modes (import, I/O, parse,
    type) rather than ``except Exception`` — see
    ``_load_max_repair_iterations`` for the same rationale.
    """
    from ...config import load_project_yaml

    try:
        data, _src = load_project_yaml(project_root)
    except (ImportError, OSError, ValueError, TypeError, AttributeError) as exc:
        logger.warning(
            "Failed to load project config for git_merge_timeout: %s "
            "— using default %d",
            exc, _DEFAULT_GIT_MERGE_TIMEOUT,
        )
        return _DEFAULT_GIT_MERGE_TIMEOUT
    if not data:
        return _DEFAULT_GIT_MERGE_TIMEOUT
    merge_data = data.get("merge", {})
    if not isinstance(merge_data, dict):
        return _DEFAULT_GIT_MERGE_TIMEOUT
    raw = merge_data.get("git_merge_timeout")
    if raw is None:
        return _DEFAULT_GIT_MERGE_TIMEOUT
    try:
        val = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "merge.git_merge_timeout=%r is not a valid integer; "
            "using default %d",
            raw, _DEFAULT_GIT_MERGE_TIMEOUT,
        )
        return _DEFAULT_GIT_MERGE_TIMEOUT
    if val < 1:
        logger.warning(
            "merge.git_merge_timeout=%d is below 1; using default %d",
            val, _DEFAULT_GIT_MERGE_TIMEOUT,
        )
        return _DEFAULT_GIT_MERGE_TIMEOUT
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
        failure_reason: FailureReason = FailureReason.GUARDRAIL_REPAIR_FAILED,
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
        failure_reason: FailureReason = FailureReason.GUARDRAIL_REPAIR_STALLED,
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
            failure_reason=FailureReason.GUARDRAIL_REPAIR_EXHAUSTED,
        )


class EmptyRepoError(RuntimeError):
    """The repository has no commits (empty repo)."""


class DetachedHeadError(RuntimeError):
    """HEAD is detached (not on a branch)."""


class ShallowRepoError(RuntimeError):
    """The repository is a shallow clone."""


class UnsupportedRepoStateError(RuntimeError):
    """The repository is in an unsupported state (submodule, LFS, etc.)."""


def _check_repo_state(project_root: Path) -> None:
    """Fail-fast for unsupported repository states (K5/K6).

    Raises:
        EmptyRepoError: when the repo has no commits.
        DetachedHeadError: when HEAD is not on a branch.
        ShallowRepoError: when the repo is a shallow clone.
        UnsupportedRepoStateError: when submodules or sparse-checkout
            are active (these can silently bypass guardrails or lose
            runtime-sync data).
    """
    # Empty repo: no commits yet
    result = _run_git(
        project_root, "rev-parse", "--verify", "HEAD",
        check=False, timeout=15,
    )
    if result.returncode != 0:
        raise EmptyRepoError(
            "Repository has no commits. Cannot merge into an empty repo."
        )

    # Detached HEAD
    result = _run_git(
        project_root, "rev-parse", "--abbrev-ref", "HEAD",
        check=False, timeout=15,
    )
    if result.returncode == 0 and result.stdout.strip() == "HEAD":
        raise DetachedHeadError(
            "HEAD is detached. Checkout a branch before merging."
        )

    # Shallow clone
    result = _run_git(
        project_root, "rev-parse", "--is-shallow-repository",
        check=False, timeout=15,
    )
    if result.returncode == 0 and result.stdout.strip() == "true":
        raise ShallowRepoError(
            "Shallow clone is not supported for se3 merge."
        )

    # Submodule check: active submodules can produce silent runtime-sync
    # data loss (files checked out in the submodule dir are invisible to
    # the parent repo's sync logic).
    result = _run_git(
        project_root, "submodule", "status",
        check=False, timeout=15,
    )
    if result.returncode == 0 and result.stdout.strip():
        raise UnsupportedRepoStateError(
            "Active git submodules detected. Submodules can cause silent "
            "runtime-sync data loss during merge. Deinitialize submodules "
            "or use --force to proceed at your own risk."
        )

    # Sparse-checkout check: if se3/specs/** is excluded, guardrails
    # would silently pass because _get_changed_spec_files returns empty.
    # We do a *targeted* check (not just "is sparse-checkout active?")
    # so a user with sparse-checkout that DOES include se3/specs/** is
    # not unnecessarily blocked.  The detection has two phases:
    #   1. ``git sparse-checkout list`` returns active patterns (cone or
    #      non-cone).  An empty list => sparse-checkout inactive.
    #   2. When patterns are present, we treat sparse-checkout as
    #      *active* and ask git directly whether ``se3/specs`` is part
    #      of the working tree.  ``git ls-files --error-unmatch
    #      se3/specs`` returns rc=0 only when at least one file under
    #      se3/specs is currently checked out — i.e., the path is part
    #      of the active sparse cone.  When rc != 0 (typically code
    #      1 with "did not match any file" or similar), we raise.
    # Edge case: a brand-new repo without any se3/specs file would
    # falsely flag as excluded even when the cone permits it.  In
    # practice se3-managed repos always have at least one spec file
    # committed, but to keep the check fail-closed without breaking
    # bootstrapping we additionally consult the cone patterns: when the
    # patterns explicitly mention "se3/specs" we treat it as included
    # regardless of ls-files results.
    result = _run_git(
        project_root, "sparse-checkout", "list",
        check=False, timeout=15,
    )
    if result.returncode == 0:
        patterns_text = result.stdout.strip()
        if patterns_text:
            # Sparse-checkout is active.  Decide whether se3/specs is in
            # the active set.  Two signals (either is sufficient):
            #   (a) explicit pattern mentions ``se3/specs`` (safe for
            #       both cone and non-cone modes — e.g. a cone-mode user
            #       listing "se3" or "se3/specs" both include it; a
            #       non-cone-mode user with pattern "/se3/specs/**"
            #       similarly).
            #   (b) ``git ls-files se3/specs`` returns at least one
            #       checked-out file under that path.
            patterns = [
                line.strip().lstrip("/")
                for line in patterns_text.splitlines()
                if line.strip()
            ]
            # In cone mode, a directory pattern includes all descendants
            # so any pattern that is se3/specs or a parent (se3, "" for
            # root cone, "*") is sufficient.  Be conservative: accept
            # the obvious matches.
            included_by_pattern = False
            for p in patterns:
                normalised = p.rstrip("/")
                if normalised in ("", "*", "se3", "se3/specs") or \
                        normalised.startswith("se3/specs"):
                    included_by_pattern = True
                    break
                # Cone mode descendant: pattern is se3/specs/foo etc.
                # That doesn't include se3/specs itself but a sibling.
                # In that case ls-files will tell us the truth.

            included_by_lsfiles = False
            if not included_by_pattern:
                ls_result = _run_git(
                    project_root, "ls-files",
                    "--error-unmatch", "se3/specs",
                    check=False, timeout=15,
                )
                included_by_lsfiles = (ls_result.returncode == 0)

            if not (included_by_pattern or included_by_lsfiles):
                raise UnsupportedRepoStateError(
                    "Sparse-checkout is active and excludes se3/specs/**. "
                    "Spec guardrails would be silently bypassed. Re-include "
                    "se3/specs in your sparse-checkout patterns or use "
                    "--force to proceed at your own risk."
                )


# MergeReport is imported from result_model (typed successor).
# The ``failure_reason`` field type is ``Optional[Union[str, FailureReason]]``
# — assignments are NOT auto-converted between the two forms.  Code that
# assigns the legacy string form (e.g. ``FailureReason.X.legacy_string``)
# stores a string; code that assigns the enum directly stores the enum.
# Consumers that need typed access SHOULD use the
# ``MergeReport.failure_reason_enum`` property, which normalises both
# forms to ``FailureReason``.  ``to_legacy_dict`` accepts either form.
# Do NOT assume an assigned string has been wrapped to enum or vice
# versa — assign whichever form the call site already uses, and read
# typed values via the property.


class _RecordingNullHumanCallWriter(HumanCallWriter):
    """Human-call writer that records escalations instead of writing files.

    The redesign retires the orchestrator's self-written human-call files: a
    library caller (:func:`integrate`) wants an escalation surfaced on the
    returned :class:`MergeResult` (``pending_human=True`` plus the recorded
    payload) and decides the flow-control itself — a flow step drives
    PAUSED/confirm/resume, the ``se3 merge`` CLI uses exit codes. So in library
    mode nothing lands in ``se3/calls/``.

    The write methods keep :class:`HumanCallWriter`'s return contract (a
    ``Path`` under ``se3/calls/``) so the orchestrator's existing
    ``if report.human_call_file:`` truthiness checks and exception plumbing are
    unchanged — the path is simply never created on disk. ``print_instructions``
    becomes a no-op (no terminal side effects in library mode).
    """

    def __init__(self, project_root: Path) -> None:
        super().__init__(project_root)
        # Recorded escalations, so the caller can render/inspect what would
        # have been written without touching the filesystem.
        self.recorded_calls: list[dict] = []

    def write_call(
        self,
        context,
        resolution,
        decision,
        *,
        options=None,
        instructions_override=None,
        call_file_name=None,
        strategy=None,
    ) -> Path:
        branch = getattr(context, "theirs_branch", "") or ""
        name = call_file_name or _generate_call_filename("merge", branch)
        call_file = self.project_root / "se3" / "calls" / name
        self.recorded_calls.append(
            {"type": "conflict", "branch": branch, "call_file": call_file}
        )
        return call_file

    def write_guardrail_call(
        self,
        branch,
        violations,
        pre_merge_sha,
        call_type="guardrail_violation",
        iteration_count=None,
    ) -> Path:
        call_file = self.project_root / "se3" / "calls" / _generate_call_filename(
            "merge", f"{branch}_guardrail",
        )
        self.recorded_calls.append(
            {
                "type": call_type,
                "branch": branch,
                "violations": violations,
                "pre_merge_sha": pre_merge_sha,
                "call_file": call_file,
            }
        )
        return call_file

    def print_instructions(self, call_file: Path) -> None:
        # Library mode: no terminal output — the caller owns presentation.
        return None


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

    **Thread-safety contract:**

    A single ``MergeOrchestrator`` instance is **NOT** thread-safe and MUST
    NOT be reused across threads. The orchestrator carries per-branch
    mutable state on the instance — most importantly
    ``_last_branch_repair_ran`` and ``_last_branch_repair_used_amend``,
    set inside ``_run_guardrails`` (per branch) and read inside
    ``_verify_post_merge_conditions`` (also per branch). The only reason
    these instance attributes are safe today is the single-threaded
    serial loop in ``execute()``: each branch resets the flags before
    its merge runs, and the next branch never overlaps with the
    previous one.

    Currently safe because:
      * ``execute()`` acquires a process-wide :class:`MergeLock`.
      * The branch loop is serial — ``_merge_single_branch`` is awaited
        synchronously before the next branch begins.
      * No internal concurrency mechanism (threading, asyncio,
        multiprocessing) is used inside the orchestrator.

    Future contributors who introduce inner concurrency (e.g. parallel
    git operations across branches, async LLM calls overlapping with
    git work) MUST replace these instance flags with per-call locals
    or a context-managed scope, otherwise post-condition routing will
    silently use the wrong branch's amend flag and ``allow_fixup_parent``
    will be applied to the wrong commit shape — re-introducing the
    silent-merge-loss class of bugs A11/A12 was added to catch.

    A single orchestrator instance is intended to be created per merge
    invocation and discarded after ``execute()`` returns. Re-using an
    instance across multiple ``execute()`` calls is allowed (the per-
    branch reset clears state correctly) but never across threads.
    """

    def __init__(
        self,
        project_root: Path,
        strategy: str = "fast",
        delete_merged: bool = True,
        strict_runtime_sync: bool = False,
        acquire_lock: bool = True,
        suppress_human_call: bool = False,
    ) -> None:
        self.project_root = project_root
        # Validate via MergeStrategy.from_str so the removed
        # ``default`` / ``robust`` names produce the migration-friendly
        # error message rather than a generic "unknown value".
        self.strategy = MergeStrategy.from_str(strategy)
        self.delete_merged = delete_merged
        self.strict_runtime_sync = strict_runtime_sync
        # When True (default), ``execute()`` wraps its body in a
        # :class:`MergeLock` so concurrent ``execute()`` calls within
        # the same process — and concurrent CLI processes that share
        # the project root — serialise on the on-disk lock file.
        # The CLI wrapper (``merge_cmd.run_merge``) already acquires
        # the lock for early validation and then constructs the
        # orchestrator with ``acquire_lock=False`` so the lock is
        # not double-acquired (flock is recursive on the same fd but
        # the surrounding context manager would surface
        # ``MergeLockBusy`` if a second flock fd were attempted).
        # Tests that intentionally exercise concurrent ``execute()``
        # calls leave the default ``True`` so they observe lock-busy
        # signaling.
        self.acquire_lock = acquire_lock
        self.log_file: Optional[Path] = None
        self._log_lines: list[str] = []
        # K2: per-LLM-call trace shared across ConflictResolver and
        # GuardrailRepairer so every LLM invocation issued during this
        # merge run lands in a single jsonl trace under
        # ``se3/logs/llm/``.  Started lazily by subcomponents.
        from ...commands.merge.llm_trace import LLMTrace

        self._llm_trace = LLMTrace(project_root)
        # K7 / A13: shared LLMCaller across ConflictResolver and
        # GuardrailRepairer so prompt cache, quota, and retry state are
        # reused within a single merge run.  Constructed lazily on first
        # access so tests that don't exercise LLM calls avoid any
        # startup cost, and any import-time or config-read errors are
        # deferred until the LLM is actually needed.
        self._llm_caller: Optional[Any] = None
        self._llm_caller_initialized = False
        self._resolver = ConflictResolver(
            project_root,
            llm_caller=self._lazy_llm_caller,
            llm_trace=self._llm_trace,
        )
        self._decider = StrategyDecider()
        # Library mode (``suppress_human_call``) swaps in a recording writer so
        # escalations surface on the returned MergeResult rather than as files
        # under se3/calls/. Default False keeps the legacy CLI/step behaviour —
        # and every existing test — byte-for-byte unchanged.
        self.suppress_human_call = suppress_human_call
        self._human_writer = (
            _RecordingNullHumanCallWriter(project_root)
            if suppress_human_call
            else HumanCallWriter(project_root)
        )
        self._guardrails = MergeGuardrailsCheck(project_root)
        self._repairer = GuardrailRepairer(
            project_root,
            llm_caller=self._lazy_llm_caller,
            llm_trace=self._llm_trace,
        )
        self._git_merge_timeout = _load_git_merge_timeout(project_root)
        self._last_stall_iteration_count: Optional[int] = None
        # A10: load max repair iterations from se3.yaml so users can
        # tune ``merge.guardrail_repair.max_iterations`` without
        # re-deploying the orchestrator.  Falls back to the module
        # default on any read or parse failure (logged inside the
        # loader).
        self._max_repair_iterations = _load_max_repair_iterations(project_root)
        # Defense-in-depth: the loader already clamps to >=1, but a future
        # refactor (or a test that monkeypatches the loader to return 0)
        # could bypass that clamp and leave the for-loop in
        # ``_run_fast_repair_loop`` with an empty range — the
        # exhausted-path block then references ``iteration`` outside the
        # for-loop, raising UnboundLocalError instead of producing a
        # clean failure.  Re-clamp here so the orchestrator is robust to
        # any code path that produces a non-positive value.
        if self._max_repair_iterations < 1:
            logger.warning(
                "Loaded _max_repair_iterations=%d is below 1; clamping to "
                "the module default to keep the repair loop well-defined",
                self._max_repair_iterations,
            )
            self._max_repair_iterations = _DEFAULT_MAX_REPAIR_ITERATIONS
        # Per-branch flag: True when guardrail repair created a fix-up
        # (or amend) commit on top of the merge commit during the
        # current branch.  Gates ``allow_fixup_parent`` in the post-
        # condition check so a stray hook commit on top of HEAD on a
        # never-repaired branch still trips ``silent_merge_loss``.
        self._last_branch_repair_ran: bool = False
        # Track whether the repair used amend (True) or fix-up (False).
        # Only fix-up mode requires allow_fixup_parent=True because HEAD
        # is a single-parent commit on top of the merge; amend mode
        # keeps HEAD as the merge commit itself.
        self._last_branch_repair_used_amend: bool = False
        # Per-branch flag: True when issue-ID reconciliation appended a
        # fix-up commit (single parent) on top of the merge commit for the
        # current branch. Counted alongside the repair fix-up when the
        # post-aggregation topology check walks back to the merge commit.
        self._last_branch_reconcile_left_fixup: bool = False
        # Per-execute flag: True when SemVer aggregation took the
        # ``amend=False`` path (HEAD already published) and produced a
        # new single-parent commit on top of the merge commit.  Any
        # post-aggregation HEAD topology re-check MUST pass
        # ``allow_fixup_parent=True`` to account for this layout.
        # Stays False when the amend=True path was taken (or no
        # aggregation ran at all).
        self._aggregation_used_fixup: bool = False
        # Cumulative fix-up depth between HEAD and the merge commit
        # after aggregation finishes.  Composed of:
        #   * +1 if the LAST branch's guardrail repair created a fix-up
        #     commit (not amend) on top of the merge commit, AND
        #   * +1 if SemVer aggregation took the amend=False path and
        #     stacked another commit on top.
        # Consumed by the post-aggregation HEAD topology check via
        # ``max_fixup_depth=`` so depth=2 ([bump → fix-up → merge])
        # passes correctly.
        self._aggregation_fixup_depth: int = 0
        # Pre-merge intent snapshot, populated by ``execute()``. The set holds
        # the flow_ids of unconsumed intents already on master before this
        # merge; ``_snapshot_ok`` records whether that snapshot actually
        # completed. Default True so a direct unit-test call of
        # ``_merged_tree_has_version_intents`` (which never runs ``execute()``)
        # exercises the introduced-intent logic on the assumption of a good
        # snapshot. ``execute()`` resets ``_snapshot_ok`` to False before
        # capturing and re-arms it only on success, so a read fault (empty
        # pre_merge_sha / probe error) correctly degrades to the legacy path.
        self._pre_merge_intent_ids: set[str] = set()
        self._pre_merge_snapshot_ok: bool = True

    @property
    def recorded_escalations(self) -> list[dict]:
        """Escalations recorded in library mode instead of written to disk.

        Change C retires the orchestrator's self-written flow management: in
        library mode (``suppress_human_call=True``) the human-call writer is a
        :class:`_RecordingNullHumanCallWriter` that *records* each escalation
        (conflict / guardrail) rather than writing an ``se3/calls/`` file or
        printing terminal instructions. The caller owns flow-control and reads
        the escalation off the returned :class:`MergeResult` (the CLI turns it
        into an exit code; a flow step drives PAUSED/confirm/resume). In the
        legacy non-suppressed mode the writer writes files as before, so this
        returns an empty list — nothing was *recorded* because everything was
        *written*.
        """
        return list(getattr(self._human_writer, "recorded_calls", []))

    def _log(
        self,
        message: str,
        level: int = logging.INFO,
        *,
        exc_info: bool = False,
    ) -> None:
        """Append a line to the internal log buffer and the logger.

        ``exc_info``: when ``True``, the current exception's traceback
        is appended to the buffered log line AND passed to the
        ``logger.log`` call so post-mortem analysis sees the full
        stack instead of a one-line ``str(exc)``. Use this in any
        ``except Exception`` block where the exception type or stack
        carries diagnostic value (rollback chains, version aggregation
        crashes, cleanup failures).
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}"
        if exc_info:
            # Capture and append the full traceback to the buffered
            # log so the on-disk log file carries the same stack
            # information as the in-memory logger output. ``traceback``
            # is imported at module top.
            import traceback as _tb
            tb_text = _tb.format_exc()
            if tb_text and tb_text.strip() and tb_text.strip() != "NoneType: None":
                line = f"{line}\n{tb_text.rstrip()}"
        self._log_lines.append(line)
        logger.log(level, message, exc_info=exc_info)

    def _write_log(self) -> None:
        """Flush the log buffer to se3/logs/merge_<ts>.log with fsync."""
        logs_dir = self.project_root / "se3" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.log_file = logs_dir / f"merge_{ts}.log"
        content = "\n".join(self._log_lines) + "\n"
        # B13 fix: fsync for durability so the log survives crash/power-loss.
        # Both the file content (via fd fsync) AND the directory entry
        # (via parent-dir fsync) must be durable — without the parent
        # fsync, a crash between content-durable and entry-durable can
        # leave the file invisible after recovery.  This matches the
        # pattern used by ``version_aggregator._atomic_write_text`` and
        # ``human_call._atomic_write_json``.
        with open(self.log_file, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        try:
            dir_fd = os.open(str(logs_dir), os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            # On platforms or filesystems that do not support
            # O_DIRECTORY (e.g. some Windows variants in WSL), skip the
            # parent-directory fsync — the file content is already
            # durable, which is the primary durability concern.
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            try:
                os.close(dir_fd)
            except OSError:
                pass

    @property
    def _lazy_llm_caller(self) -> Optional[Any]:
        """Lazy construction of the shared LLMCaller instance.

        Built on first access and cached so ConflictResolver and
        GuardrailRepairer share the same instance within a merge run.
        """
        if not self._llm_caller_initialized:
            self._llm_caller_initialized = True
            try:
                from ..llm_caller import LLMCaller as _LLMCaller

                self._llm_caller = _LLMCaller(
                    project_root=self.project_root,
                    step_type="merge",
                )
            except Exception as exc:
                logger.warning(
                    "LLMCaller construction failed (project_root=%s): %s "
                    "— per-component fallback will lose shared prompt-cache "
                    "and quota state across ConflictResolver / GuardrailRepairer.",
                    self.project_root, exc,
                )
                self._llm_caller = None
        return self._llm_caller

    def _populate_unattempted(self, report: MergeReport, branches: list[str]) -> None:
        """Compute unattempted branches when the loop exits early and log them."""
        if report.failed_branch and report.failed_branch in branches:
            failed_idx = branches.index(report.failed_branch)
            report.unattempted_branches = branches[failed_idx + 1:]
            if report.unattempted_branches:
                self._log(
                    f"Unattempted branches: {', '.join(report.unattempted_branches)}"
                )

    def _record_outcome(
        self,
        report: MergeReport,
        branch: str,
        result: Any,
        *,
        merge_commit_sha: Optional[str] = None,
        warnings_repaired: bool = False,
        failure_detail: Optional[str] = None,
        git_merge_succeeded: bool = False,
    ) -> None:
        """Append a typed :class:`MergeOutcome` for *branch* to ``report.outcomes``.

        G1[2]: every branch processed by the orchestrator now produces
        exactly one ``MergeOutcome``.  Bucket population
        (``newly_merged_branches``, ``already_ancestor_branches``,
        ``merged_branches``) remains the caller's responsibility — this
        helper does NOT call ``report.add_outcome()`` because the
        orchestrator already maintains those lists manually and we want
        to avoid double-appending during the deprecation window.

        ``result`` accepts either a legacy string (e.g. ``"merged"``,
        ``"merge_conflict"``) or a :class:`FailureReason` enum value
        (returned from some exception handlers as ``exc.failure_reason``).

        ``git_merge_succeeded`` flags branches whose git merge step
        itself was successful but whose downstream sub-step (runtime
        sync, version aggregation) later failed.  G3 semantic-alignment
        fix: the legacy ``merged_branches`` bucket records the branch
        because git did merge it; the typed outcome carries
        ``success=False`` to reflect the overall failure but
        ``git_merge_succeeded=True`` so consumers can see that the
        branch IS in the merged bucket.  Always pair this with
        ``merge_commit_sha`` so the typed view is fully self-describing.
        """
        from ...commands.merge.failure_reason import (
            FailureReason,
            from_legacy_string,
        )

        # Skip if we already recorded an outcome for this branch — defensive
        # against future call sites that fire after the legacy paths populate
        # report.outcomes some other way.
        if any(o.branch == branch for o in report.outcomes):
            return

        # Normalise FailureReason enum results to their legacy string
        # form so downstream branching uses a single source of truth.
        if isinstance(result, FailureReason):
            result_str = result.legacy_string
        else:
            result_str = result

        if result_str == "merged":
            report.outcomes.append(
                MergeOutcome(
                    branch=branch,
                    success=True,
                    merge_commit_sha=merge_commit_sha,
                    warnings_repaired=warnings_repaired,
                    git_merge_succeeded=True,
                )
            )
            return
        if result_str == "already_merged":
            report.outcomes.append(
                MergeOutcome(
                    branch=branch,
                    success=True,
                    already_ancestor=True,
                    warnings_repaired=warnings_repaired,
                    git_merge_succeeded=True,
                )
            )
            return

        # Failure or pending-human path — translate the legacy string to
        # a FailureReason enum.
        reason, parsed_detail = from_legacy_string(result_str)
        report.outcomes.append(
            MergeOutcome(
                branch=branch,
                success=False,
                failure_reason=reason,
                failure_detail=failure_detail or parsed_detail,
                merge_commit_sha=merge_commit_sha,
                git_merge_succeeded=git_merge_succeeded,
            )
        )

    def _verify_post_merge_conditions(
        self,
        branch: str,
        *,
        already_ancestor: bool,
        report: MergeReport,
        allow_fixup_parent: bool = False,
    ) -> Optional[str]:
        """Verify post-merge conditions before declaring success.

        Defect B1 fix: every "successfully merged" return path MUST
        execute ancestry and (when applicable) merge-commit-shape checks.
        If a guardrail rollback or any other intermediate step silently
        lost the merge commit, this helper catches it and routes the
        outcome to a typed ``silent_merge_loss`` failure rather than
        falsely reporting success.

        Args:
            allow_fixup_parent: When ``True``, accept HEAD^1 as the merge
                commit (fix-up layout produced by guardrail repair).
                Callers MUST only set this on paths where guardrail
                repair could legitimately have created a fix-up commit
                on top of the merge.

        Returns ``None`` when checks pass; otherwise returns the
        failure reason string (suitable for ``_merge_single_branch``
        callers to propagate via the state-machine return value).
        """
        try:
            assert_branch_merged(self.project_root, branch, timeout=15)
            if not already_ancestor:
                assert_head_is_merge_commit(
                    self.project_root,
                    branch,
                    allow_fixup_parent=allow_fixup_parent,
                    timeout=15,
                )
        except PostConditionViolated as exc:
            self._log(
                f"Post-condition violation for '{branch}': {exc}",
                level=logging.ERROR,
            )
            if exc.reason == FailureReason.POSTCOND_BRANCH_UNRESOLVABLE:
                report.failure_reason = (
                    FailureReason.POSTCOND_BRANCH_UNRESOLVABLE.legacy_string
                )
                return "silent_merge_loss_branch_unresolvable"
            report.failure_reason = FailureReason.SILENT_MERGE_LOSS.legacy_string
            return "silent_merge_loss"
        except subprocess.TimeoutExpired as exc:
            self._log(
                f"Post-condition check for '{branch}' timed out: {exc}. "
                f"Treating as fail-closed: a 15s timeout on git merge-base "
                f"or rev-parse typically signals filesystem/git corruption — "
                f"silent success here would mask exactly the case where the "
                f"merge may have been silently lost.",
                level=logging.ERROR,
            )
            report.failure_reason = FailureReason.POSTCOND_CHECK_TIMEOUT.legacy_string
            return "postcond_check_timeout"
        except (OSError, subprocess.SubprocessError) as exc:
            # G3 fix (high): catch operational/IO errors here so the
            # diagnostic context is preserved at the merge step instead
            # of bubbling up into the caller and being relabelled as a
            # generic UNEXPECTED failure several frames removed from
            # the actual merge. Programming-error classes
            # (AttributeError from a refactor regression, AssertionError,
            # TypeError, etc.) intentionally still propagate so they
            # surface as crashes rather than typed failures.
            self._log(
                f"Post-condition check for '{branch}' raised "
                f"{type(exc).__name__}: {exc}. Treating as silent "
                f"merge loss because we cannot prove the post-merge "
                f"invariants without a clean check.",
                level=logging.ERROR,
                exc_info=True,
            )
            report.failure_reason = FailureReason.SILENT_MERGE_LOSS.legacy_string
            report.failure_detail = (
                f"Post-condition check for '{branch}' raised "
                f"{type(exc).__name__}: {exc}"
            )
            return "silent_merge_loss"
        return None

    def _record_branch_bump(
        self,
        branch: str,
        pre_merge_sha: str,
        branch_bumps: list[BumpType],
        report: Optional[MergeReport] = None,
        previously_merged_branches: Optional[list[str]] = None,
    ) -> None:
        """Infer and record the SemVer bump for a successfully merged branch.

        Called from both the normal ``"merged"`` path and the runtime-sync
        failure paths (B12 fix: a successful git merge must still contribute
        its bump even when later runtime sync fails).

        When *report* is provided, transient failures (subprocess timeout,
        infer-result parse error, etc.) are recorded on
        ``report.bump_inference_failures`` so operators can see when a
        branch's contribution to the aggregated bump was silently dropped.

        ``previously_merged_branches`` (optional, recommended): the list of
        branches successfully merged earlier in the same ``execute()``
        invocation.  When supplied, ``_record_branch_bump`` checks whether
        the computed merge-base lies on the tip of any earlier branch — a
        signal that the current branch was rebased on top of an earlier
        one (cross-branch dependency).  In that case the inferred bump
        reflects the version delta from an *intermediate* state, not from
        the original pre-merge HEAD, and the value can be subtly wrong.
        The branch's bump is still contributed (avoiding a silent drop)
        but a WARNING is logged so operators can audit the result.
        """
        def _record_failure(reason: str) -> None:
            if report is not None:
                report.bump_inference_failures.append((branch, reason))

        try:
            merge_base_result = _run_git(
                self.project_root,
                "merge-base",
                pre_merge_sha,
                branch,
                check=False,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            self._log(
                f"merge-base timed out for '{branch}' — skipping bump inference"
            )
            _record_failure("merge-base timeout")
            return
        if merge_base_result.returncode != 0:
            self._log(
                f"merge-base failed for '{branch}': "
                f"{merge_base_result.stderr.strip()} — skipping bump inference"
            )
            _record_failure(
                f"merge-base failed: {merge_base_result.stderr.strip()}"
            )
            return
        merge_base_sha = merge_base_result.stdout.strip()

        # Cross-branch dependency detection.  The spec wording assumes the
        # branches in the argument list are independent: each branch's
        # bump is the end-to-end version diff from its merge-base with
        # the ORIGINAL pre_merge_sha.  When branchN was rebased on top of
        # branchN-1, ``git merge-base pre_merge_sha branchN`` may return
        # a commit that lies on branchN-1's tip (or further inside its
        # history), and ``read_version_at_ref(merge_base)`` would then
        # report whatever version was at that intermediate commit rather
        # than the version that was on the current branch before
        # branchN-1 merged.  Detect and log the situation so operators
        # can audit the aggregated bump rather than silently absorbing
        # subtly wrong inputs.
        if previously_merged_branches:
            for earlier in previously_merged_branches:
                if earlier == branch:
                    continue
                try:
                    is_anc = _run_git(
                        self.project_root,
                        "merge-base",
                        "--is-ancestor",
                        merge_base_sha,
                        earlier,
                        check=False,
                        timeout=10,
                    )
                except subprocess.TimeoutExpired:
                    # Best-effort warning — don't fail bump inference
                    # over a timed-out ancestry check.
                    continue
                if is_anc.returncode == 0:
                    self._log(
                        f"WARNING: merge-base for '{branch}' "
                        f"({merge_base_sha[:8]}) lies in '{earlier}' "
                        f"history — '{branch}' may have been rebased on "
                        f"top of '{earlier}'.  Bump inference treats "
                        f"branches as independent, so the contributed "
                        f"bump may not reflect '{branch}'s true "
                        f"end-to-end change from the original pre-merge "
                        f"version.  Audit the aggregated bump if exact "
                        f"semantics matter.",
                        level=logging.WARNING,
                    )
                    break
        try:
            infer_result = infer_branch_bump(
                self.project_root,
                branch,
                merge_base_sha,
            )
            if infer_result.bump is not None:
                branch_bumps.append(infer_result.bump)
                self._log(
                    f"Inferred bump for '{branch}': {infer_result.bump.value}"
                )
            else:
                self._log(
                    f"Bump inference skipped for '{branch}': {infer_result.reason}"
                )
                # When infer_branch_bump returns a None bump it has
                # already classified the reason (e.g. version unchanged
                # end-to-end) — that path is NOT an error, so we do NOT
                # record it on bump_inference_failures.  Only transient
                # errors (timeout, exception) are recorded.
        except subprocess.TimeoutExpired as exc:
            # Surface git timeouts inside infer_branch_bump as a
            # distinct, recordable failure.  Without this branch the
            # caller's broad ``except Exception`` swallowed timeouts
            # silently, leading to an under-bumped aggregate version
            # with no signal in MergeReport.
            self._log(
                f"infer_branch_bump timed out for '{branch}': {exc} — "
                f"bump inference dropped",
                level=logging.ERROR,
            )
            _record_failure(f"infer timeout: {exc}")
        except Exception as exc:
            self._log(
                f"Failed to infer bump for '{branch}': {exc}",
                level=logging.ERROR,
            )
            _record_failure(f"infer error: {exc}")

    def _merged_tree_has_version_intents(self) -> bool:
        """True when this merge carries an as-yet-unreconciled VersionIntent.

        De-versioning split (2026-07-06): a merged-in worktree branch emits a
        :class:`VersionIntent` and the merge-side ``reconcile()`` step/entry —
        which now runs unconditionally after ``integrate()`` in both entry
        points — owns the version decision from it. Running the legacy per-branch
        aggregation too would DOUBLE-BUMP an intent-carrying branch whose tip
        also advanced the version file: ``infer_branch_bump`` applies the file
        delta, then ``reconcile()`` applies the intent on top. So the legacy path
        must stand down whenever THIS merge brings in unconsumed intents.

        ``include_consumed=False`` is load-bearing: every reconcile commits the
        consumed intent JSON into master and never deletes it, so probing with
        ``include_consumed=True`` would see that permanent residue and suppress
        aggregation for ALL later merges — including a pure legacy branch that
        advances the version file directly and carries no intent, which would
        then land verbatim with no bump. The just-merged branch's own intent is
        still unconsumed at this point (reconcile runs after execute/integrate),
        so it is counted here while long-consumed historical intents are not;
        pure legacy (no-intent) branches keep aggregating exactly as before.

        Filtering against ``_pre_merge_intent_ids`` is the second load-bearing
        guard: an unconsumed intent left on master by a *different* flow (Flow A
        finished ``merge_integrate`` but has not yet run ``version_reconcile``)
        is NOT contributed by the branches this merge is bringing in. Counting it
        would wrongly stand aggregation down for a concurrent pure-legacy
        ``se3 merge``, so the legacy branch would land with no bump. Only intents
        absent from master's pre-merge tree (i.e. introduced by these branches)
        are counted. Any read fault degrades to ``False`` (fall back to the
        legacy path) rather than aborting the merge.

        A failure to snapshot the pre-merge intent set (``_pre_merge_snapshot_ok``
        is False — e.g. ``git rev-parse HEAD`` stalled so ``pre_merge_sha`` was
        empty, or ``intent_flow_ids_at_ref`` raised) is itself a read fault: an
        empty ``_pre_merge_intent_ids`` in that case does NOT mean "no
        pre-existing intents", so treating it as such would wrongly count an
        unrelated concurrent flow's leftover intent as introduced by these
        branches and stand aggregation down for a pure-legacy branch. Degrade to
        ``False`` (legacy aggregation keeps bumping exactly as before).
        """
        if not getattr(self, "_pre_merge_snapshot_ok", False):
            return False
        try:
            from ..version_intent import collect_intents

            pre_existing = getattr(self, "_pre_merge_intent_ids", None) or set()
            introduced = [
                intent
                for intent in collect_intents(
                    self.project_root, include_consumed=False
                )
                if intent.flow_id not in pre_existing
            ]
            return bool(introduced)
        except Exception:  # noqa: BLE001 - intent probing must never abort the merge
            return False

    def execute(self, branches: list[str]) -> MergeReport:
        """Execute sequential merge of all branches.

        Args:
            branches: Branch names to merge, in order.

        Returns:
            MergeReport summarizing the outcome.

        Concurrency: when ``self.acquire_lock`` is ``True`` (default),
        the merge body is wrapped in a blocking :class:`MergeLock` so two
        ``execute()`` calls that share the same project root — even
        within the same Python process — serialise on the on-disk lock
        file, the second queueing until the first releases instead of
        stomping on each other's working tree, index, and runtime sync
        targets.  Callers that have already acquired the lock externally
        (e.g. ``merge_cmd.run_merge``) construct the orchestrator with
        ``acquire_lock=False`` to avoid re-acquiring.
        """
        if not self.acquire_lock:
            return self._execute_inner(branches)

        from ...commands.merge.merge_lock import (
            MergeLock,
            MergeLockBusy,
            MergeLockStale,
        )

        # Defensive same-process re-entry detection: if the caller forgot
        # to pass ``acquire_lock=False`` while themselves holding a
        # MergeLock at the CLI layer, attempting a second flock against
        # the same lock file from the same PID has platform-dependent
        # semantics (Linux: same OFD = success, different OFD = EAGAIN).
        # Consult the merge_lock module-level registry; when this
        # process already holds the lock for the same project, short-
        # circuit to a no-op so the merge body runs under the externally
        # -held lock instead of spuriously raising MergeLockBusy.
        from ...commands.merge.merge_lock import is_lock_held_in_process
        if is_lock_held_in_process(self.project_root):
            self._log(
                "Merge lock already held in this process; orchestrator "
                "skips re-acquisition."
            )
            return self._execute_inner(branches)

        try:
            # Blocking acquisition (main-worktree mutex): a concurrent
            # holder causes this call to queue until the lock is free
            # rather than failing fast. The MergeLockBusy / MergeLockStale
            # handlers below are retained as defensive fallbacks only —
            # blocking acquisition does not raise them.
            with MergeLock(self.project_root, blocking=True):
                return self._execute_inner(branches)
        except MergeLockBusy as exc:
            self._log(
                f"Merge lock is held by another process "
                f"(pid={exc.holder_pid}); refusing to start.",
                level=logging.ERROR,
            )
            report = MergeReport()
            report.success = False
            report.failure_reason = FailureReason.LOCK_BUSY.legacy_string
            report.failure_detail = (
                f"Merge lock {exc.lock_file} held by pid={exc.holder_pid}"
            )
            self._write_log()
            report.log_file = self.log_file
            return report
        except MergeLockStale as exc:
            self._log(
                f"Merge lock appears stale (pid={exc.holder_pid}); "
                f"refusing to break it automatically. Remove "
                f"{exc.lock_file} manually.",
                level=logging.ERROR,
            )
            report = MergeReport()
            report.success = False
            report.failure_reason = FailureReason.LOCK_STALE.legacy_string
            report.failure_detail = (
                f"Merge lock {exc.lock_file} appears stale (pid={exc.holder_pid})"
            )
            self._write_log()
            report.log_file = self.log_file
            return report

    def _assert_version_bumped(
        self, report: "MergeReport", expected_version: str, *, context: str
    ) -> None:
        """Run :func:`assert_version_bumped` and map exceptions onto the report.

        Centralises the post-aggregation version-bump check so the success
        path and the already-at-target path can't drift apart on a future
        edit. ``context`` distinguishes log lines (e.g. ``""`` vs.
        ``"already at target"``); a non-empty value is appended in
        parentheses.
        """
        suffix = f" ({context})" if context else ""
        # Preserve a more-specific failure reason that an upstream caller
        # has already recorded (e.g. VERSION_HIGHER_THAN_TARGET or
        # VERSION_ALREADY_AT_TARGET).  These reasons describe the *why*
        # of the aggregation no-op; demoting them to the generic
        # POSTCOND_VERSION_NOT_BUMPED here would lose the diagnostic
        # signal callers depend on.
        _SPECIFIC_VERSION_REASONS = {
            FailureReason.VERSION_HIGHER_THAN_TARGET.legacy_string,
            FailureReason.VERSION_ALREADY_AT_TARGET.legacy_string,
        }
        try:
            assert_version_bumped(self.project_root, expected_version)
            self._log(f"Version bump post-condition passed{suffix}")
        except PostConditionViolated as pc_exc:
            self._log(
                f"Version bump post-condition FAILED{suffix}: {pc_exc}",
                level=logging.ERROR,
            )
            report.success = False
            if report.failure_reason not in _SPECIFIC_VERSION_REASONS:
                report.failure_reason = pc_exc.reason.legacy_string
            report.version_aggregation_error = str(pc_exc)
        except (OSError, subprocess.SubprocessError, ValueError) as pc_exc:
            # G3 fix (low): narrow from bare except so a circular-import
            # regression or other structural error surfaces as a crash
            # rather than being relabelled POSTCOND_VERSION_NOT_BUMPED.
            # The postcondition import has been hoisted to the module
            # top; a future structural failure now fails at module load
            # rather than at this call site.
            self._log(
                f"Version bump post-condition FAILED{suffix} "
                f"({type(pc_exc).__name__}): {pc_exc}",
                level=logging.ERROR,
            )
            report.success = False
            if report.failure_reason not in _SPECIFIC_VERSION_REASONS:
                report.failure_reason = (
                    FailureReason.POSTCOND_VERSION_NOT_BUMPED.legacy_string
                )
            report.version_aggregation_error = str(pc_exc)

    def _execute_inner(self, branches: list[str]) -> MergeReport:
        """The actual merge body — runs inside the merge lock when one is held."""
        report = MergeReport()

        # I1 defense-in-depth: refuse an empty branch list at the
        # orchestrator boundary. The CLI layer (`run_merge` ->
        # `validate_branch_names`) already rejects this upstream, but a
        # programmatic caller bypassing the CLI would otherwise see the
        # for-loop iterate zero times and a misleading
        # ``report.success = True`` returned with no merged branches.
        # Fail-fast here so every entry point is protected.
        if not branches:
            self._log(
                "Refusing to execute with empty branch list — at least one "
                "branch is required.",
                level=logging.ERROR,
            )
            report.success = False
            report.failure_reason = FailureReason.NO_BRANCHES.legacy_string
            report.failure_detail = (
                "se3 merge invoked with no branches; nothing to do."
            )
            self._write_log()
            report.log_file = self.log_file
            return report

        # K5/K6: fail-fast for unsupported repository states (empty repo,
        # detached HEAD, shallow clone) BEFORE any merge work.
        try:
            _check_repo_state(self.project_root)
        except (EmptyRepoError, DetachedHeadError, ShallowRepoError, UnsupportedRepoStateError) as exc:
            self._log(
                f"Repository state check failed: {exc}",
                level=logging.ERROR,
            )
            report.success = False
            # G3 fix: use an explicit class -> FailureReason map rather
            # than deriving the failure reason from
            # ``type(exc).__name__``. The previous
            # ``type(exc).__name__.replace("Error", "").lower()`` string
            # silently mutated whenever any of these exception classes
            # was renamed; downstream consumers (tests, CLI rendering,
            # operators searching log files) would then see a different
            # failure_reason for the same underlying state.  The map is
            # the single source of truth and is also exercised by the
            # legacy-string round-trip in failure_reason._LEGACY_STRING_MAP.
            _repo_state_reason_map = {
                EmptyRepoError: FailureReason.REPO_EMPTY,
                DetachedHeadError: FailureReason.REPO_DETACHED_HEAD,
                ShallowRepoError: FailureReason.REPO_SHALLOW,
                UnsupportedRepoStateError: FailureReason.REPO_UNSUPPORTED_STATE,
            }
            report.failure_reason = _repo_state_reason_map[type(exc)].legacy_string
            self._write_log()
            report.log_file = self.log_file
            return report
        except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError) as exc:
            # Defensive: _check_repo_state runs internal git invocations
            # whose subprocess errors and OS-level failures (missing .git,
            # filesystem I/O issues) would otherwise propagate out of
            # execute() with no typed MergeReport, leaving the CLI to
            # render a raw stack trace.  Treat these as a structured
            # failure instead.
            #
            # We deliberately do NOT add a trailing ``except Exception``
            # catch-all here: programming errors (AttributeError, TypeError,
            # NameError from a refactor regression) MUST surface as crashes
            # rather than being silently masked as FailureReason.UNEXPECTED.
            self._log(
                f"Repository state check raised "
                f"{type(exc).__name__}: {exc}",
                level=logging.ERROR,
            )
            report.success = False
            report.failure_reason = FailureReason.UNEXPECTED.legacy_string
            report.failure_detail = (
                f"_check_repo_state raised {type(exc).__name__}: {exc}"
            )
            self._write_log()
            report.log_file = self.log_file
            return report

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

        # Dirty pre-flight (lock held, repo-state validated, BEFORE the
        # pre_merge_sha capture below): auto-commit self-managed issue state so
        # a branch that also touched se3/issues/.next_id can actually START its
        # merge and route the divergence through NextIdResolver. Placing this
        # ahead of the pre_merge_sha capture makes the "chore: sync issue state"
        # commit part of the rollback baseline — _rollback_to can never discard
        # it — and ensures the pre-merge version read happens on the post-sync
        # HEAD. Dirty tracked files outside the self-managed whitelist fail loud.
        if not self._preflight_dirty_tracked_files(report, branches):
            self._write_log()
            report.log_file = self.log_file
            return report

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

        # Snapshot which version-intents already exist on master BEFORE this
        # merge. Only intents INTRODUCED by the branches being merged should
        # suppress legacy version aggregation; a leftover unconsumed intent from
        # an unrelated flow (still awaiting its own version_reconcile) must NOT
        # stand aggregation down for a pure legacy branch that carries no intent.
        # ``_pre_merge_snapshot_ok`` distinguishes a genuinely empty pre-merge
        # intent set (snapshot succeeded, nothing outstanding) from a read fault
        # (empty ``pre_merge_sha`` or an intent-probe error). Only the former may
        # let _merged_tree_has_version_intents count introduced intents; a fault
        # must degrade to the legacy path, so the flag stays False unless the
        # snapshot actually completed.
        self._pre_merge_intent_ids: set[str] = set()
        self._pre_merge_snapshot_ok: bool = False
        if pre_merge_sha:
            try:
                from ..version_intent import intent_flow_ids_at_ref

                self._pre_merge_intent_ids = intent_flow_ids_at_ref(
                    self.project_root, pre_merge_sha
                )
                self._pre_merge_snapshot_ok = True
            except Exception:  # noqa: BLE001 - intent probing must never abort the merge
                self._pre_merge_intent_ids = set()
                self._pre_merge_snapshot_ok = False
        # B5 fix: wrap read_version_at_ref so a TimeoutExpired (transient
        # git stall) is logged distinctly from a missing version file.
        # Without this, the inner subprocess.TimeoutExpired propagates
        # uncaught and is later swallowed in a higher-level handler,
        # making "file missing" indistinguishable from "git timed out
        # reading the ref".
        pre_merge_version = None
        if pre_merge_sha:
            try:
                pre_merge_version = read_version_at_ref(
                    self.project_root, pre_merge_sha,
                )
            except subprocess.TimeoutExpired as exc:
                self._log(
                    f"read_version_at_ref timed out for pre-merge SHA "
                    f"{pre_merge_sha[:8]}: {exc} — treating as unavailable"
                )
                pre_merge_version = None
            except subprocess.CalledProcessError as exc:
                self._log(
                    f"read_version_at_ref failed for pre-merge SHA "
                    f"{pre_merge_sha[:8]}: {exc} — treating as unavailable"
                )
                pre_merge_version = None
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
            self._last_branch_repair_ran = False
            self._last_branch_repair_used_amend = False
            self._last_branch_reconcile_left_fixup = False
            self._log(f"--- Merging branch: {branch} ---")

            result = self._merge_single_branch(branch, report)

            # G1[2]: typed per-branch outcome.  Captured immediately after
            # the result string is determined so every branch produces
            # exactly one MergeOutcome appended to report.outcomes — even
            # the failure paths below that ``return`` early.  Bucket
            # population (newly_merged_branches/already_ancestor_branches/
            # merged_branches) remains the manual append pattern; the
            # outcome is a parallel typed record, not a replacement.
            outcome_sha: Optional[str] = None
            if result == "merged":
                # Read HEAD to capture the merge commit SHA for typed
                # consumers; failure to read is non-fatal — outcome still
                # records success=True with sha=None.
                try:
                    head_result = _run_git(
                        self.project_root, "rev-parse", "HEAD",
                        check=False, timeout=15,
                    )
                    if head_result.returncode == 0:
                        outcome_sha = head_result.stdout.strip()
                except subprocess.TimeoutExpired:
                    pass
            elif result in (
                "runtime_sync_collision",
                "runtime_sync_os_error",
                "runtime_sync_timeout",
            ):
                # G3 semantic-alignment: git merge succeeded but a
                # downstream sub-step (runtime sync) failed.  Record
                # the merge commit SHA so the typed view matches the
                # legacy ``merged_branches`` bucket, then we set
                # ``git_merge_succeeded=True`` below.
                try:
                    head_result = _run_git(
                        self.project_root, "rev-parse", "HEAD",
                        check=False, timeout=15,
                    )
                    if head_result.returncode == 0:
                        outcome_sha = head_result.stdout.strip()
                except subprocess.TimeoutExpired:
                    pass
            # G3: when git merge succeeded but a downstream sub-step
            # failed (runtime sync collision/OS error/timeout), flag
            # the typed outcome so consumers can distinguish "git
            # merge failed" from "git merged but post-merge step
            # failed" (the legacy ``merged_branches`` bucket records
            # the latter as a successful git merge).
            outcome_git_merge_succeeded = result in (
                "merged",
                "already_merged",
                "runtime_sync_collision",
                "runtime_sync_os_error",
                "runtime_sync_timeout",
            )
            self._record_outcome(
                report,
                branch,
                result,
                merge_commit_sha=outcome_sha,
                warnings_repaired=self._last_branch_repair_ran,
                git_merge_succeeded=outcome_git_merge_succeeded,
            )

            if result == "merged" or result == "already_merged":
                self._log(f"Branch '{branch}' merged successfully")
                report.merged_branches.append(branch)
                # Defect I3: split bucket so the CLI can render
                # newly-merged branches separately from already-ancestor
                # ones. ``_merge_single_branch`` returns "already_merged"
                # only for branches whose tips were already reachable
                # from HEAD before this invocation.
                if result == "already_merged":
                    report.already_ancestor_branches.append(branch)
                else:
                    # When the branch's merge ran guardrail repair (fast
                    # mode), surface it via the dedicated
                    # ``merged_with_warnings`` bucket so downstream
                    # consumers (CLI rendering, ``to_legacy_dict``) can
                    # tell repaired-merge branches from clean-merge ones.
                    # The branch is also appended to ``merged_branches``
                    # (above) so the legacy aggregate view continues to
                    # report it; the new bucket is in addition to, not
                    # in place of, that aggregate.
                    if self._last_branch_repair_ran:
                        report.merged_with_warnings.append(branch)
                    else:
                        report.newly_merged_branches.append(branch)
                if pre_merge_sha and pre_merge_version:
                    # Compute merge-base for end-to-end diff semantics
                    if result == "already_merged":
                        # B2 fix: already-merged branches MUST be excluded
                        # from bump aggregation. The branch's contribution
                        # to the version was already absorbed by the prior
                        # merge — re-computing it here double-counts the
                        # bump. We still walk up to find the base ref so
                        # ``effective_pre_merge_version`` is updated to
                        # reflect the lowest pre-this-branch version (so
                        # the *other* branches' bumps apply to the
                        # correct base), but we do NOT call
                        # ``infer_branch_bump`` for this branch and we
                        # ``continue`` to the next branch immediately
                        # afterward.
                        base_ref, merged_commit = self._find_base_ref_for_already_merged(
                            branch, pre_merge_sha,
                        )
                        if base_ref is None:
                            self._log(
                                f"Already-merged '{branch}' contributes no "
                                f"bump (no base ref found) — skipping bump "
                                f"inference"
                            )
                            continue
                        # Defensive: if the version at HEAD already differs
                        # from the version before this branch was merged, the
                        # branch's version change is already in the tree and
                        # we must not double-bump.
                        # B5 fix: wrap each read_version_at_ref so a
                        # transient git timeout is distinguishable from a
                        # missing version file.
                        try:
                            head_version = read_version_at_ref(
                                self.project_root, pre_merge_sha,
                            )
                        except subprocess.TimeoutExpired as exc:
                            self._log(
                                f"read_version_at_ref timed out reading head "
                                f"version for already-merged '{branch}': {exc} "
                                f"— skipping bump inference"
                            )
                            continue
                        except subprocess.CalledProcessError as exc:
                            self._log(
                                f"read_version_at_ref failed reading head "
                                f"version for already-merged '{branch}': {exc} "
                                f"— treating as unavailable"
                            )
                            head_version = None
                        try:
                            base_version = read_version_at_ref(
                                self.project_root, base_ref,
                            )
                        except subprocess.TimeoutExpired as exc:
                            self._log(
                                f"read_version_at_ref timed out reading base "
                                f"version for already-merged '{branch}': {exc} "
                                f"— skipping bump inference"
                            )
                            continue
                        except subprocess.CalledProcessError as exc:
                            self._log(
                                f"read_version_at_ref failed reading base "
                                f"version for already-merged '{branch}': {exc} "
                                f"— treating as unavailable"
                            )
                            base_version = None
                        if (
                            head_version is not None
                            and base_version is not None
                            and head_version != base_version
                        ):
                            # The branch's version change is already in HEAD.
                            # Use the version before this branch as the
                            # effective pre-merge version (lowest wins when
                            # multiple already-merged branches exist).
                            try:
                                base_v = Version.parse(base_version)
                                eff_v = (
                                    Version.parse(effective_pre_merge_version)
                                    if effective_pre_merge_version
                                    else None
                                )
                                if eff_v is None or base_v < eff_v:
                                    effective_pre_merge_version = base_version
                            except ValueError:
                                # One or both versions are not parseable as SemVer.
                                # Do NOT fall back to lexicographic string comparison
                                # (e.g. '1.10.0' < '1.2.0' would be wrong).
                                if effective_pre_merge_version is None:
                                    effective_pre_merge_version = base_version
                            self._log(
                                f"Version change for already-merged '{branch}' "
                                f"already in HEAD ({base_version} -> {head_version}) "
                                f"— using {base_version} as effective pre-merge version"
                            )
                        # B2 fix: explicitly DO NOT contribute to bump
                        # aggregation for already-merged branches. The
                        # version change is already in the tree and
                        # double-counting it would over-bump.
                        self._log(
                            f"Skipping bump inference for already-merged "
                            f"'{branch}' (excluded from aggregation per B2)"
                        )
                        continue
                    else:
                        self._record_branch_bump(
                            branch, pre_merge_sha, branch_bumps, report,
                            previously_merged_branches=list(
                                report.merged_branches
                            ),
                        )
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
                    report.failure_reason = FailureReason.MERGE_CONFLICT.legacy_string
                report.version_aggregation_skipped = True
                self._populate_unattempted(report, branches)
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result == "context_build_failed":
                # Distinct from "conflict": the merge was aborted because
                # build_conflict_context itself raised, so we never even
                # got to the resolver.  Surface the real cause so that
                # an operator does not waste time looking for unresolved
                # text conflicts in the working tree.
                self._log(
                    f"Branch '{branch}' aborted: failed to build conflict "
                    f"context (the merge could not be prepared for resolution)"
                )
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — re-run after resolving"
                    )
                report.success = False
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = FailureReason.CONFLICT_CONTEXT_FAILED.legacy_string
                report.version_aggregation_skipped = True
                self._populate_unattempted(report, branches)
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result in ("silent_merge_loss", "silent_merge_loss_branch_unresolvable"):
                # B1 fix: post-condition detected that the merge commit was
                # silently lost between merge-time and report-time
                # (rollback / amend bug, dropped reference, etc.). Surface
                # the failure rather than report success with a missing
                # commit.
                #
                # Both variants share the same dispatch handling because they
                # are both fail-closed signals from
                # ``_verify_post_merge_conditions``:
                #   - "silent_merge_loss": the merge commit is missing on HEAD
                #   - "silent_merge_loss_branch_unresolvable": the branch
                #     name itself can no longer be resolved post-merge
                # The specific reason is preserved in ``report.failure_reason``
                # which was set by the caller before returning.
                self._log(
                    f"Branch '{branch}' post-condition violation: silent merge loss",
                    level=logging.ERROR,
                )
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — repository may be in an inconsistent state"
                    )
                report.success = False
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = FailureReason.SILENT_MERGE_LOSS.legacy_string
                report.version_aggregation_skipped = True
                self._populate_unattempted(report, branches)
                self._write_log()
                report.log_file = self.log_file
                return report
            elif result == "postcond_check_timeout":
                # Post-condition check (merge-base / rev-parse) timed out.
                # Treated as fail-closed because a 15s timeout on these
                # primitives typically signals filesystem/git corruption —
                # exactly the scenario where the merge may have been
                # silently lost and silent success would be most dangerous.
                self._log(
                    f"Branch '{branch}' post-condition check timed out — "
                    f"reporting failure (fail-closed)",
                    level=logging.ERROR,
                )
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — repository may be in an inconsistent state"
                    )
                report.success = False
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = FailureReason.POSTCOND_CHECK_TIMEOUT.legacy_string
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
                    report.failure_reason = FailureReason.PENDING_HUMAN.legacy_string
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
                report.failure_reason = FailureReason.GUARDRAIL_VIOLATION.legacy_string
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
                report.failure_reason = FailureReason.GUARDRAIL_VIOLATION_CALL_FAILED.legacy_string
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
                report.failure_reason = FailureReason.GUARDRAIL_VIOLATION_NO_ROLLBACK.legacy_string
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
                    report.failure_reason = FailureReason.GUARDRAIL_REPAIR_STALLED.legacy_string
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
                report.failure_reason = FailureReason.ROLLBACK_FAILED.legacy_string
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
                    report.failure_reason = FailureReason.MERGE_ABORT_FAILED.legacy_string
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
                    report.failure_reason = FailureReason.MERGE_FAILED.legacy_string
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
                    report.failure_reason = FailureReason.RESOLUTION_COMMIT_TIMEOUT.legacy_string
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
                    report.failure_reason = FailureReason.INCOMPLETE_RESOLUTION_CALL_FAILED.legacy_string
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
                    report.failure_reason = FailureReason.HUMAN_CALL_WRITE_FAILED.legacy_string
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
                    report.failure_reason = FailureReason.FAST_ABORT.legacy_string
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
                    report.failure_reason = FailureReason.BINARY_FILE_CONFLICT.legacy_string
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
                    report.failure_reason = FailureReason.RESOLUTION_VALIDATION_FAILED.legacy_string
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
                    report.failure_reason = FailureReason.RESOLUTION_WRITE_FAILED.legacy_string
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
                    report.failure_reason = FailureReason.RESOLUTION_COMMIT_FAILED.legacy_string
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
                if branch not in report.merged_branches:
                    report.merged_branches.append(branch)
                    report.newly_merged_branches.append(branch)
                # B12: successful git merge must still contribute its bump.
                if pre_merge_sha:
                    self._record_branch_bump(
                        branch, pre_merge_sha, branch_bumps, report,
                        previously_merged_branches=list(
                            report.merged_branches
                        ),
                    )
                self._log(
                    f"WARNING: Branch '{branch}' merge commit is on HEAD but "
                    f"runtime sync failed. On retry, include '{branch}' again "
                    f"so the already-merged path can complete runtime sync."
                )
                if report.merged_branches:
                    self._log(
                        f"{len(report.merged_branches)} successful merge(s) "
                        f"will still feed version aggregation; subsequent "
                        f"branches were not attempted"
                    )
                report.success = False
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = FailureReason.RUNTIME_SYNC_COLLISION.legacy_string
                break
            elif result == "runtime_sync_os_error":
                self._log(
                    f"Branch '{branch}' runtime sync OS error — stopping merge sequence"
                )
                if branch not in report.merged_branches:
                    report.merged_branches.append(branch)
                    report.newly_merged_branches.append(branch)
                # B12: successful git merge must still contribute its bump.
                if pre_merge_sha:
                    self._record_branch_bump(
                        branch, pre_merge_sha, branch_bumps, report,
                        previously_merged_branches=list(
                            report.merged_branches
                        ),
                    )
                self._log(
                    f"WARNING: Branch '{branch}' merge commit is on HEAD but "
                    f"runtime sync failed. On retry, include '{branch}' again "
                    f"so the already-merged path can complete runtime sync."
                )
                if report.merged_branches:
                    self._log(
                        f"{len(report.merged_branches)} successful merge(s) "
                        f"will still feed version aggregation; subsequent "
                        f"branches were not attempted"
                    )
                report.success = False
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = FailureReason.RUNTIME_SYNC_OS_ERROR.legacy_string
                break
            elif result == "runtime_sync_timeout":
                self._log(
                    f"Branch '{branch}' runtime sync timed out — stopping merge sequence"
                )
                if branch not in report.merged_branches:
                    report.merged_branches.append(branch)
                    report.newly_merged_branches.append(branch)
                # B12: successful git merge must still contribute its bump.
                if pre_merge_sha:
                    self._record_branch_bump(
                        branch, pre_merge_sha, branch_bumps, report,
                        previously_merged_branches=list(
                            report.merged_branches
                        ),
                    )
                self._log(
                    f"WARNING: Branch '{branch}' merge commit is on HEAD but "
                    f"runtime sync failed. On retry, include '{branch}' again "
                    f"so the already-merged path can complete runtime sync."
                )
                if report.merged_branches:
                    self._log(
                        f"{len(report.merged_branches)} successful merge(s) "
                        f"will still feed version aggregation; subsequent "
                        f"branches were not attempted"
                    )
                report.success = False
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = FailureReason.RUNTIME_SYNC_TIMEOUT.legacy_string
                break
            elif result == "inconsistent_repair_state":
                self._log(
                    f"CRITICAL: Branch '{branch}' entered inconsistent repair state. "
                    f"The repository may contain an unrolled-back repair commit on HEAD. "
                    f"Subsequent branches are NOT attempted. Manual intervention required.",
                    level=logging.ERROR,
                )
                if report.merged_branches:
                    self._log(
                        f"Version not bumped despite {len(report.merged_branches)} "
                        f"successful merge(s) — re-run after resolving"
                    )
                report.success = False
                report.failed_branch = branch
                if not report.failure_reason:
                    report.failure_reason = FailureReason.INCONSISTENT_REPAIR_STATE.legacy_string
                report.version_aggregation_skipped = True
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
                    report.failure_reason = FailureReason.UNEXPECTED.legacy_string
                report.version_aggregation_skipped = True
                self._populate_unattempted(report, branches)
                self._write_log()
                report.log_file = self.log_file
                return report

        # All branches merged successfully (no runtime-sync or other failure)
        if report.failed_branch is None:
            report.success = True
            self._log(f"All {len(branches)} branch(es) merged successfully")
            self._log(f"Merged: {', '.join(report.merged_branches)}")
        else:
            # The loop exited via a `break` from one of the runtime_sync_*
            # branches.  The git merge for the failed branch DID succeed
            # (the branch was recorded in merged_branches and contributed
            # a bump), but a downstream step (runtime sync) failed.
            # Subsequent branches were not attempted.  Surface the partial
            # state honestly rather than claiming full success.
            self._log(
                f"Merge sequence halted at branch "
                f"'{report.failed_branch}' "
                f"({report.failure_reason}); "
                f"{len(report.merged_branches)} of {len(branches)} "
                f"branch(es) merged before halt"
            )
            if report.merged_branches:
                self._log(f"Merged before halt: {', '.join(report.merged_branches)}")

        # SemVer aggregation: apply max bump to pyproject.toml and amend.
        # Aggregation runs whenever at least one branch produced a successful
        # merge commit and contributed a bump (B12 fix: runtime-sync failures
        # after a successful git merge must NOT short-circuit aggregation).
        # branch_bumps is only populated for branches whose git merge
        # succeeded, so it serves as the gate: if empty, there is nothing
        # to aggregate.
        #
        # De-versioning split: when merged-in branches carry a VersionIntent the
        # merge-side reconcile owns the version decision, so the legacy path is
        # suppressed to avoid double-bumping (see
        # ``_merged_tree_has_version_intents``).
        has_version_intents = self._merged_tree_has_version_intents()
        if (
            branch_bumps
            and effective_pre_merge_version
            and not has_version_intents
        ):
            report.effective_pre_merge_version = effective_pre_merge_version
            self._log("Aggregating SemVer bumps from merged branches")
            # Track whether aggregation produced a fix-up-style HEAD
            # (single-parent commit on top of the merge commit) so that
            # any code added after this block can pass the right
            # ``allow_fixup_parent`` flag to ``assert_head_is_merge_commit``.
            # This flag is set ONLY when ``amend=False`` aggregation
            # actually succeeded — the amend=True path keeps HEAD as the
            # merge commit so the flag stays False there.
            self._aggregation_used_fixup = False
            self._aggregation_fixup_depth = 0
            # Pre-aggregation: each single-parent fix-up commit the LAST
            # branch left on top of the merge commit adds one to the depth
            # the post-aggregation HEAD topology check must walk back to
            # reach the merge commit. Two independent sources can stack:
            # a fast-mode guardrail repair fix-up, and an issue-ID
            # reconciliation fix-up (they commit in that order, both on top
            # of the merge commit). An amend-mode repair leaves HEAD as the
            # merge commit and contributes no depth.
            if self._last_branch_repair_ran and not self._last_branch_repair_used_amend:
                self._aggregation_fixup_depth += 1
            if self._last_branch_reconcile_left_fixup:
                self._aggregation_fixup_depth += 1
            try:
                is_published = self._is_head_published()
                if is_published:
                    self._log(
                        "WARNING: HEAD has been published to a remote. "
                        "Creating a new commit for version aggregation instead of amending."
                    )
                # NOTE on HEAD topology after this call:
                #   * amend=True path  → HEAD remains the merge commit
                #     (parent_count >= 2). assert_head_is_merge_commit
                #     would still hold for any code that runs later in
                #     this method.
                #   * amend=False path → a NEW single-parent commit is
                #     placed on top of the merge commit. HEAD now has
                #     parent_count == 1; assert_head_is_merge_commit
                #     would fail UNLESS callers pass
                #     ``allow_fixup_parent=True`` so HEAD^1 (the merge
                #     commit) is checked instead.  ``self._aggregation_used_fixup``
                #     is set True in that case so any post-aggregation
                #     re-check can route to the right branch.
                agg = aggregate_and_apply(
                    self.project_root,
                    branch_bumps,
                    effective_pre_merge_version,
                    amend=not is_published,
                )
                if agg.success:
                    if not is_published:
                        # amend=True path was actually taken.  The amend
                        # rewrites HEAD in place, so the depth between
                        # HEAD and the merge commit is unchanged from
                        # whatever the repair phase left:
                        #   * No prior fix-up → HEAD is the merge commit
                        #     (depth 0 ⇒ allow_fixup_parent=False).
                        #   * Prior repair fix-up → HEAD is amended
                        #     fix-up, merge commit at HEAD^1
                        #     (depth 1 ⇒ allow_fixup_parent=True,
                        #     max_fixup_depth=1).
                        self._aggregation_used_fixup = (
                            self._aggregation_fixup_depth >= 1
                        )
                    else:
                        # amend=False path was taken — a NEW commit was
                        # placed on top of HEAD.  The merge commit moves
                        # one more parent away.
                        self._aggregation_fixup_depth += 1
                        self._aggregation_used_fixup = True
                    report.final_version = agg.new_version
                    if agg.bump_type is not None and getattr(agg, "bump_applied", False):
                        report.bump_type = agg.bump_type.value
                    self._log(
                        f"Version aggregated: {effective_pre_merge_version} → {agg.new_version} "
                        f"({agg.bump_type.value if agg.bump_type else 'unknown'})"
                    )
                    # Post-aggregation HEAD topology re-verification.
                    # Prevents a stray hook commit (or future code path)
                    # from silently producing a HEAD that no longer
                    # carries the merge-commit shape.  When the amend=False
                    # branch ran, HEAD itself is a single-parent commit
                    # so we walk back one parent (allow_fixup_parent=True).
                    if report.merged_branches:
                        try:
                            # Use the last merged branch as the diagnostic
                            # context — the check itself is global to HEAD,
                            # the branch name only enriches the error
                            # detail when it fails.
                            # Walk back up to the cumulative fix-up
                            # depth (max 1 for repair-only or
                            # aggregation-only, max 2 for repair+
                            # aggregation stacked).  Cap min depth at 1
                            # when ``allow_fixup_parent`` is True so the
                            # signature contract holds.
                            assert_head_is_merge_commit(
                                self.project_root,
                                report.merged_branches[-1],
                                allow_fixup_parent=self._aggregation_used_fixup,
                                max_fixup_depth=max(
                                    1, self._aggregation_fixup_depth
                                ),
                            )
                            self._log(
                                "Post-aggregation HEAD topology check passed"
                                + (
                                    f" (fix-up layout depth="
                                    f"{self._aggregation_fixup_depth})"
                                    if self._aggregation_used_fixup
                                    else ""
                                )
                            )
                        except PostConditionViolated as pc_exc:
                            self._log(
                                f"Post-aggregation HEAD topology check FAILED: {pc_exc}",
                                level=logging.ERROR,
                            )
                            report.success = False
                            report.failure_reason = pc_exc.reason.legacy_string
                            report.failure_detail = (
                                f"HEAD topology re-verification after version "
                                f"aggregation failed: {pc_exc}"
                            )
                        except Exception as pc_exc:
                            # Defensive: any unexpected exception during the
                            # re-verification becomes a typed failure rather
                            # than crashing out of execute().
                            # G3: include traceback so post-mortem analysis
                            # sees the full stack rather than a one-line
                            # ``str(exc)``.
                            self._log(
                                f"Post-aggregation HEAD topology check raised "
                                f"{type(pc_exc).__name__}: {pc_exc}",
                                level=logging.ERROR,
                                exc_info=True,
                            )
                            report.success = False
                            report.failure_reason = (
                                FailureReason.POSTCOND_HEAD_NOT_MERGE_COMMIT.legacy_string
                            )
                            report.failure_detail = (
                                f"Post-aggregation HEAD check raised "
                                f"{type(pc_exc).__name__}: {pc_exc}"
                            )
                    # A11/silent-merge-loss symmetry: re-verify EACH merged
                    # branch is still in HEAD's ancestry after the
                    # aggregation amend/commit. The HEAD topology check
                    # only confirms HEAD itself is a merge commit; it does
                    # NOT confirm that all branches recorded in
                    # ``report.merged_branches`` are still reachable from
                    # HEAD. If the post-aggregation amend rewrote HEAD
                    # with a stale parent (e.g. a future amend bug, a
                    # hook that resets HEAD, a manual rebase), individual
                    # branches could fall out of HEAD's ancestry while
                    # ``report.success`` stayed True — exactly the
                    # silent-merge-loss class A11 was added to catch,
                    # only at a later orchestration phase. The check is
                    # gated on ``report.success`` already being True so
                    # that an earlier failure path doesn't overwrite a
                    # more specific failure_reason with a less informative
                    # ancestry verdict.
                    if report.success and report.merged_branches:
                        try:
                            for merged_branch in report.merged_branches:
                                assert_branch_merged(
                                    self.project_root,
                                    merged_branch,
                                    timeout=15,
                                )
                            self._log(
                                "Post-aggregation branch-ancestry check "
                                "passed for all merged branches"
                            )
                        except PostConditionViolated as pc_exc:
                            self._log(
                                f"Post-aggregation branch-ancestry check "
                                f"FAILED: {pc_exc}",
                                level=logging.ERROR,
                            )
                            report.success = False
                            report.failure_reason = pc_exc.reason.legacy_string
                            report.failure_detail = (
                                f"Branch-ancestry re-verification after version "
                                f"aggregation failed: {pc_exc}"
                            )
                        except subprocess.TimeoutExpired as pc_exc:
                            self._log(
                                f"Post-aggregation branch-ancestry check "
                                f"timed out: {pc_exc}",
                                level=logging.ERROR,
                            )
                            report.success = False
                            report.failure_reason = (
                                FailureReason.POSTCOND_CHECK_TIMEOUT.legacy_string
                            )
                            report.failure_detail = (
                                f"Branch-ancestry re-verification after version "
                                f"aggregation timed out: {pc_exc}"
                            )
                        except Exception as pc_exc:
                            # Defensive: any unexpected exception during
                            # the re-verification becomes a typed failure
                            # rather than crashing out of execute().
                            self._log(
                                f"Post-aggregation branch-ancestry check raised "
                                f"{type(pc_exc).__name__}: {pc_exc}",
                                level=logging.ERROR,
                                exc_info=True,
                            )
                            report.success = False
                            report.failure_reason = (
                                FailureReason.SILENT_MERGE_LOSS.legacy_string
                            )
                            report.failure_detail = (
                                f"Post-aggregation branch-ancestry check raised "
                                f"{type(pc_exc).__name__}: {pc_exc}"
                            )
                    # B1 post-condition: verify the version file actually
                    # contains the new version after amend/commit.
                    self._assert_version_bumped(
                        report, agg.new_version, context=""
                    )
                elif getattr(agg, "version_already_at_target", False):
                    # Version already at target (e.g. a prior branch's
                    # merge brought pyproject.toml to the computed
                    # target, or the disk is already above it).  Treat as
                    # warning, not a skip.  When the on-disk version is
                    # strictly *higher* than the computed target, flag it
                    # in MergeReport so operators can investigate without
                    # grepping logs.
                    report.final_version = agg.new_version
                    # bump_type is intentionally NOT set when the bump was
                    # skipped — downstream consumers reading report.bump_type
                    # should only see bumps that were actually applied.
                    if getattr(agg, "version_higher_than_target", False):
                        report.version_higher_than_target = True
                        self._log(
                            f"WARNING: On-disk version is HIGHER than the "
                            f"aggregated target. {agg.error}",
                            level=logging.WARNING,
                        )
                        # Fail-loud: a higher-than-target disk version is an
                        # anomalous state (stale pre_merge_version, manual bump
                        # that skipped the computed target, etc.).  Treat as a
                        # non-success so the anomaly is impossible to overlook.
                        report.success = False
                        report.failure_reason = (
                            FailureReason.VERSION_HIGHER_THAN_TARGET.legacy_string
                        )
                        report.version_aggregation_error = agg.error
                    else:
                        # G3 fix (critical): equal-version case has two
                        # legitimate shapes and one suspicious shape. Only
                        # the suspicious one should fail loud; the others
                        # are normal warnings.
                        #
                        # Legitimate (continue success):
                        #   * A branch tip's pyproject.toml already
                        #     contained the bumped version. After ``git
                        #     merge`` the working tree carries that bumped
                        #     value, and the aggregator's computed target
                        #     happens to coincide. Detected by
                        #     ``report.newly_merged_branches`` being
                        #     non-empty: at least one branch produced a
                        #     real merge commit, so the merge sequence
                        #     contributed work to HEAD even if the version
                        #     write itself is a no-op.
                        #
                        # Suspicious (fail loud — user incident shape):
                        #   * No newly-merged branches in this invocation
                        #     (everything was already-ancestor) AND the
                        #     disk is already at the target. This is the
                        #     "merge silently no-op'd while disk was
                        #     previously advanced manually" case the user
                        #     reported: the operator sees "Successfully
                        #     merged" alongside "Version: 4.6.1 -> 4.7.0
                        #     (HEAD already at 4.7.0 from prior merges)"
                        #     and asks why the version didn't advance.
                        had_newly_merged = bool(
                            getattr(report, "newly_merged_branches", None)
                            or [
                                o for o in report.outcomes
                                if o.success and not o.already_ancestor
                            ]
                        )
                        if had_newly_merged:
                            # Legitimate no-op: a real merge contributed
                            # to HEAD; the version coincidence is benign.
                            self._log(
                                f"Version aggregation no-op (already at "
                                f"target, but {len(report.newly_merged_branches)} "
                                f"branch(es) produced new merge commits — "
                                f"treating as benign): {agg.error}"
                            )
                        else:
                            # Suspicious: NO new commits, yet aggregator
                            # claims disk already at target. Fail loud to
                            # surface the user-incident shape.
                            self._log(
                                f"Version aggregation no-op (already at "
                                f"target AND no newly-merged branches — "
                                f"this is the silent-no-op shape): "
                                f"{agg.error}",
                                level=logging.WARNING,
                            )
                            report.success = False
                            if not report.failure_reason:
                                report.failure_reason = (
                                    FailureReason.VERSION_ALREADY_AT_TARGET.legacy_string
                                )
                                report.failure_detail = (
                                    f"Version already at target with no "
                                    f"newly-merged branches: {agg.error}"
                                )
                            report.version_aggregation_error = agg.error
                    # B1 post-condition: even when the version was already
                    # at target, verify the on-disk file matches.
                    self._assert_version_bumped(
                        report, agg.new_version, context="already at target"
                    )
                else:
                    # Generic version-aggregation failure branch. Covers
                    # write failures, file-not-found, current-version
                    # parse failures, restore failures, plus the benign
                    # "no bumps to aggregate" case. Distinguish them:
                    #   * "no bumps to aggregate" — informational skip
                    #     (per-branch bumps were not inferred). Leaves
                    #     report.success unchanged.
                    #   * everything else — fail-loud. The earlier
                    #     behavior (just record the error string and
                    #     leave report.success=True) was the user-incident
                    #     shape: the merge claimed success but the version
                    #     write silently failed.
                    report.version_aggregation_error = agg.error
                    report.version_aggregation_skipped = True
                    err = agg.error or ""
                    if err.startswith("no bumps"):
                        self._log(
                            f"Version aggregation no-op: {agg.error}"
                        )
                    else:
                        self._log(
                            f"Version aggregation failed: {agg.error}",
                            level=logging.ERROR,
                        )
                        report.success = False
                        # Preserve any earlier, more specific failure
                        # reason (e.g. a guardrails failure that already
                        # set report.failure_reason). Only overwrite
                        # when the report has not yet recorded one.
                        if not report.failure_reason:
                            report.failure_reason = (
                                FailureReason.POSTCOND_VERSION_NOT_BUMPED.legacy_string
                            )
                            report.failure_detail = (
                                f"Version aggregation failed: {agg.error}"
                            )
            except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as exc:
                # G3: narrow the catch to operational/IO error classes.
                # Programming-error classes (AssertionError, TypeError,
                # KeyError, AttributeError, etc.) are explicitly NOT
                # caught here so they propagate as crashes rather than
                # being silently converted to FailureReason.UNEXPECTED.
                # The narrow tuple is the same shape recommended for
                # other except blocks in this module that mediate
                # subprocess + filesystem operations.
                report.success = False
                report.failure_reason = FailureReason.UNEXPECTED.legacy_string
                report.failure_detail = f"Version aggregation raised: {exc}"
                report.version_aggregation_error = str(exc)
                report.version_aggregation_skipped = True
                self._log(f"Version aggregation raised: {exc}")
        else:
            report.version_aggregation_skipped = True
            if has_version_intents:
                # The merge-side reconcile step/entry owns the version decision
                # for intent-carrying branches; the legacy aggregation stands
                # down so the two deciders never both fire (double-bump guard).
                self._log(
                    "Skipping legacy version aggregation: merged branches carry "
                    "VersionIntents; the merge-side reconcile owns the version "
                    "decision"
                )
            elif report.failed_branch is not None:
                self._log(
                    f"Skipping version aggregation: branch '{report.failed_branch}' "
                    f"failed ({report.failure_reason}); aggregation requires a "
                    f"fully successful merge sequence"
                )
            elif not effective_pre_merge_version:
                self._log(
                    "Skipping version aggregation: no pre-merge version available "
                    "(pre_merge_version was unreadable at start and no branch "
                    "provided a parseable base version — likely pyproject.toml "
                    "missing or version field absent from all refs)"
                )
            elif not branch_bumps:
                self._log("Skipping version aggregation: no branches contributed bumps")

        # --delete-merged: clean up branches and worktrees
        # Cleanup runs only when the merge as a whole succeeded
        # (``report.success``).  An earlier iteration of this code OR'd
        # in ``version_higher_than_target`` so cleanup would also run
        # when the only remaining problem was an anomalous on-disk
        # version, but that produced a confusing operator UX:
        #
        #   * ``report.success = False`` (fail-loud signal for the
        #     anomaly) was set, the CLI exited 1, and the failure
        #     summary said "merge failed";
        #   * yet ``--delete-merged`` had still run, so the source
        #     branches were already gone and the operator could not
        #     re-run ``se3 merge`` to investigate the version anomaly
        #     against the original branches.
        #
        # The fix is to skip cleanup whenever ``report.success`` is
        # False — including the version-anomaly path.  The operator
        # then sees the failure summary AND the branches are still
        # available for inspection / re-run.  When the operator has
        # confirmed the on-disk version is intentional, they can
        # delete the merged branches manually with ``git branch -d``.
        cleanup_allowed = report.success
        if self.delete_merged and cleanup_allowed:
            report.cleanup_skipped = False
            self._log("Running cleanup for --delete-merged")
            cleanup = CleanupManager(self.project_root)
            try:
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
                # G3: include traceback so post-mortem analysis sees the
                # full stack rather than a one-line ``str(exc)``.
                self._log(
                    f"Cleanup failed: {exc}",
                    level=logging.WARNING,
                    exc_info=True,
                )
                # Surface the partial report (whatever cleanup completed
                # before raising) rather than a synthetic empty one.  The
                # CleanupManager publishes its in-progress report on
                # ``_current_report`` so an aborted run still tells the
                # operator which branches were actually deleted.
                # The synthetic entry surfaces as a bullet in the operator-facing
                # merge report, so it is authored UI prose and goes through the
                # catalog; only the exception itself is interpolated as data.
                aborted_reason = t(
                    "cli.merge.cleanup.aborted_reason",
                    error_type=type(exc).__name__,
                    error=exc,
                )
                partial = getattr(cleanup, "_current_report", None)
                if partial is not None:
                    report.cleanup_report = partial
                    if partial.deleted:
                        self._log(
                            f"Cleanup partially completed before failure. "
                            f"Deleted: {', '.join(partial.deleted)}"
                        )
                    # Append a synthetic entry so the exception's branch is
                    # explicitly recorded — without this, an operator who
                    # only inspects the deleted/skipped lists could miss
                    # that the cleanup ended on an exception.
                    partial.skipped_unknown_state.append(
                        ("<cleanup-aborted>", aborted_reason)
                    )
                else:
                    # Defensive: pre-loop failure (no report was published
                    # because the exception fired before delete_merged_branches
                    # entered its body).  Fall back to a synthetic report so
                    # downstream consumers always see a structured object.
                    report.cleanup_report = CleanupReport(
                        skipped_not_merged=[("<cleanup-aborted>", aborted_reason)],
                    )
        else:
            if not self.delete_merged:
                self._log("Cleanup skipped: --delete-merged not set")
            elif report.version_higher_than_target:
                # Specific message for the version-anomaly path so the
                # operator knows their --delete-merged request was
                # refused on purpose (so they can re-run / investigate
                # against the still-existing source branches) and not
                # because of a deeper failure.
                self._log(
                    "Cleanup skipped: on-disk version is higher than the "
                    "aggregated target. Source branches preserved so you "
                    "can investigate; once the version anomaly is "
                    "resolved, delete merged branches manually with "
                    "`git branch -d <branch>`."
                )
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
                if sync_report.ambiguous_issue_references:
                    # Both merge channels satisfy the same guarantee set,
                    # including "record ambiguous #old references in the merge
                    # report" — carry the runtime-sync channel's ambiguities
                    # into the report so the CLI summary and serialized report
                    # expose them exactly as the committed channel's are.
                    report.ambiguous_issue_references.extend(
                        sync_report.ambiguous_issue_references
                    )
        except RuntimeSyncCollision as exc:
            self._log(f"Runtime sync collision for '{branch}': {exc}")
            report.failure_reason = FailureReason.RUNTIME_SYNC_COLLISION.legacy_string
            report.runtime_sync_collision_path = exc.rel_path
            return "runtime_sync_collision"
        except OSError as exc:
            self._log(
                f"Runtime sync OS error for '{branch}': {exc}"
            )
            if self.strict_runtime_sync:
                report.failure_reason = FailureReason.RUNTIME_SYNC_OS_ERROR.legacy_string
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
            report.failure_reason = FailureReason.RUNTIME_SYNC_TIMEOUT.legacy_string
            return "runtime_sync_timeout"
        return None

    # ------------------------------------------------------------------
    # Committed-issue ID reconciliation (git three-way-merge channel)
    # ------------------------------------------------------------------

    def _issue_relpath(self, path: Path) -> str:
        """Repo-relative, forward-slash path for git plumbing commands."""
        return path.relative_to(self.project_root).as_posix()

    def _path_in_ref(self, ref: str, path: Path) -> bool:
        """Whether *path* existed as a committed blob at *ref*.

        Used to tell the kept side of a collision (already present on the
        current branch before the merge) from the merge-introduced side.
        """
        if not ref:
            return False
        try:
            res = _run_git(
                self.project_root,
                "cat-file",
                "-e",
                f"{ref}:{self._issue_relpath(path)}",
                check=False,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            # Fail closed: if we cannot prove the file predates the merge,
            # treat it as not-present so it is not mistaken for the keeper.
            return False
        return res.returncode == 0

    def _issue_relpaths_at_ref(self, ref: str) -> set[str]:
        """Repo-relative paths of every issue YAML *committed* at *ref*.

        The committed-issue channel must decide authorship from the git trees,
        never from the dirty working directory: globbing ``se3/issues`` sweeps
        in the user's UNTRACKED issue drafts and their uncommitted edits, which
        belong to the runtime-sync / uncommitted domain, not to this merge.
        Renumbering or committing them here would rewrite a kept-side reference
        or publish private drafts, and — if a never-committed draft were picked
        as a collision loser — a rollback could not restore it from HEAD,
        destroying the issue outright. Returns an empty set when the ref is
        empty or git cannot answer, so callers fall back to touching nothing
        rather than to the unsafe working-tree glob.
        """
        if not ref:
            return set()
        try:
            res = _run_git(
                self.project_root,
                "ls-tree",
                "-r",
                "--name-only",
                ref,
                "--",
                "se3/issues/open",
                "se3/issues/closed",
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return set()
        if res.returncode != 0:
            return set()
        return {
            line.strip()
            for line in res.stdout.splitlines()
            if line.strip().endswith(".yaml")
        }

    def _merge_authored_issue_paths(
        self, pre_merge_sha: str,
    ) -> tuple[set[str], set[str]]:
        """Issue relpaths the merge ADDED / MODIFIED, from committed git trees.

        Authorship is decided by diffing the pre-merge commit against HEAD (the
        merge commit) — two committed trees — instead of comparing the working
        directory against *pre_merge_sha*. A working-tree comparison mistakes an
        untracked draft (absent at *pre_merge_sha*) for a merge-introduced file
        and an uncommitted edit (disk != baseline) for a merge modification, and
        would then rewrite the user's ``#old`` references or stage their private
        drafts into the renumber fix-up commit. ``--no-renames`` keeps a rename
        as delete+add so the added side is classified purely by presence.

        Returns ``(added_relpaths, modified_relpaths)``; both empty when the ref
        is missing or git cannot answer, so classification touches nothing.
        """
        if not pre_merge_sha:
            return set(), set()
        try:
            res = _run_git(
                self.project_root,
                "diff",
                "--name-status",
                "--no-renames",
                pre_merge_sha,
                "HEAD",
                "--",
                "se3/issues/open",
                "se3/issues/closed",
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return set(), set()
        if res.returncode != 0:
            return set(), set()
        added: set[str] = set()
        modified: set[str] = set()
        for line in res.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status, relpath = parts[0].strip(), parts[1].strip()
            if not relpath.endswith(".yaml"):
                continue
            if status.startswith("A"):
                added.add(relpath)
            elif status.startswith("M"):
                modified.add(relpath)
        return added, modified

    def _parse_committed_issue_numeric_id(self, path: Path) -> Optional[int]:
        """Numeric identity of a committed issue file.

        The record's parsed ``id`` field is the authority — that is the number
        cross-references and ``IssueManager`` lookups resolve, so two files
        whose ``id`` fields agree collide even when their filename prefixes
        differ. The filename prefix is only the fallback for a record whose
        ``id`` is missing or unparsable.
        """
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            data = None
        return self._numeric_id_from_body(data, path.name)

    def _numeric_id_from_body(
        self, data: object, filename: str,
    ) -> Optional[int]:
        """Resolve a numeric ID from a parsed issue mapping + its filename.

        Shared by the working-tree and committed-blob readers so the ``id``
        field / ``NNN_`` filename-prefix fallback precedence is identical no
        matter which source the body came from.
        """
        if isinstance(data, dict) and data.get("id") is not None:
            try:
                return int(str(data["id"]).strip())
            except ValueError:
                pass
        match = re.match(r"^(\d+)_", filename)
        return int(match.group(1)) if match else None

    def _committed_issue_numeric_id(
        self, ref: str, path: Path,
    ) -> Optional[int]:
        """Numeric identity of an issue file AS COMMITTED at *ref*.

        Collision detection must read the ``id`` from the merge commit's tree,
        not the working tree: a tracked issue file carrying an uncommitted
        local edit to its ``id`` field would otherwise inject a
        working-tree-only number into the committed-issue collision set. That
        number is absent from the committed merge tree, so acting on it would
        renumber a genuinely-unique committed issue (and rewrite its
        references, and cut a fix-up commit) purely on the strength of a dirty
        edit — exactly the ``git commit``-tree guarantee this channel exists to
        uphold. Falls back to the filename prefix (which the merge tree carries
        too) when the committed body has no parsable ``id`` or git cannot read
        the blob.
        """
        content: Optional[str] = None
        if ref:
            try:
                res = _run_git(
                    self.project_root,
                    "show",
                    f"{ref}:{self._issue_relpath(path)}",
                    check=False,
                    timeout=15,
                )
            except subprocess.TimeoutExpired:
                res = None
            if res is not None and res.returncode == 0:
                content = res.stdout
        data: object = None
        if content is not None:
            try:
                data = yaml.safe_load(content)
            except yaml.YAMLError:
                data = None
        return self._numeric_id_from_body(data, path.name)

    def _detect_committed_id_collisions(
        self, issues_root: Path, ref: str, tracked_relpaths: set[str],
    ) -> list[tuple[int, list[Path]]]:
        """Group committed issue files by numeric ID and return collisions.

        A numeric ID owns a collision when two or more *distinct* files carry
        it. Two files can only differ by path (a single path is a single
        file), so any group of size >= 2 is a genuine "two different issues,
        one ID" collision — git already folds a byte-identical same-path
        issue into one file, so it never reaches here.

        Only files present in *tracked_relpaths* (the issue YAMLs committed at
        the merge commit) take part, and each file's grouping number is read
        from its blob AS COMMITTED at *ref* — never the working tree. Two
        different filters would let a tracked file with an uncommitted ``id``
        edit slip into the committed set under a number the merge tree never
        held. An untracked main-side draft that happens to share a number with
        a merged issue belongs to the uncommitted / runtime-sync domain, not
        this channel: renumbering it would rewrite and commit the user's
        private draft, and — if it were picked as the loser — a rollback could
        not restore it from HEAD (it was never committed), deleting the issue
        outright.
        """
        groups: dict[int, list[Path]] = {}
        for sub in ("open", "closed"):
            directory = issues_root / sub
            if not directory.exists():
                continue
            for f in sorted(directory.glob("*.yaml")):
                if self._issue_relpath(f) not in tracked_relpaths:
                    continue
                numeric_id = self._committed_issue_numeric_id(ref, f)
                if numeric_id is not None:
                    groups.setdefault(numeric_id, []).append(f)
        return [
            (numeric_id, paths)
            for numeric_id, paths in sorted(groups.items())
            if len(paths) >= 2
        ]

    def _choose_kept_issue(self, paths: list[Path], pre_merge_sha: str) -> Path:
        """Pick which colliding file keeps its ID; the rest are renumbered.

        A file that already existed at *pre_merge_sha* is the current
        branch's copy and wins over anything merge-introduced (matching
        ``adopt_issue``'s "keep the main copy, renumber the incoming one"
        semantics) — even when a pre-existing duplicate leaves SEVERAL
        survivors, the keeper is still picked among them, never from the
        incoming side. Ties (several survivors, or zero when every colliding
        file is merge-introduced) break on the lexicographically smallest
        repo-relative path so repeated runs converge on the same keeper.
        """
        survivors = [p for p in paths if self._path_in_ref(pre_merge_sha, p)]
        return min(survivors or paths, key=self._issue_relpath)

    def _keeper_lived_on_branch(self, branch: str, keep: Path) -> bool:
        """Whether the kept file also existed in the merged branch's own tree.

        When it did, the branch's store already held TWO issues under the
        colliding number (the inherited keeper plus its own new file), so a
        branch-authored ``#old`` could mean the keeper just as well as the
        renumbered loser. Fails toward True (ambiguous) when git cannot
        answer: a reference is only rewritten on proof, never on a failed
        check.
        """
        if not branch:
            return True
        try:
            res = _run_git(
                self.project_root,
                "cat-file",
                "-e",
                f"{branch}:{self._issue_relpath(keep)}",
                check=False,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            return True
        return res.returncode == 0

    def _current_max_issue_id(self) -> int:
        """Highest numeric issue ID across ``se3/issues/{open,closed}``.

        Takes the max over BOTH the filename prefix and the parsed ``id``
        field of every file: a fresh number must clear whichever is higher,
        or the renumber itself would mint a new collision (e.g. a record
        whose ``id`` exceeds every filename prefix).
        """
        issues_root = self.project_root / "se3" / "issues"
        max_id = 0
        for sub in ("open", "closed"):
            directory = issues_root / sub
            if not directory.exists():
                continue
            for f in directory.glob("*.yaml"):
                match = re.match(r"^(\d+)_", f.name)
                if match:
                    max_id = max(max_id, int(match.group(1)))
                parsed = self._parse_committed_issue_numeric_id(f)
                if parsed is not None:
                    max_id = max(max_id, parsed)
        return max_id

    @staticmethod
    def _dump_issue_yaml(data: dict) -> str:
        return yaml.dump(
            data, default_flow_style=False, allow_unicode=True, sort_keys=False,
        )

    def _renumber_committed_issue(
        self, path: Path, old_num: int,
    ) -> tuple[IssueMergeRecord, Path]:
        """Rename one colliding issue file to ``max(ID)+1`` and rewrite its ``id``.

        ONLY the rename + in-file ``id`` rewrite happen here. Cross-reference
        rewriting and the old->new trace are deferred to the caller's batch
        passes: a group can hold several incoming files sharing one old ID,
        in which case any remaining ``#old`` token is ambiguous — rewriting
        ``#old`` store-wide per renumber would silently repoint it at the
        FIRST loser's new number. Only the batch, which sees the whole group,
        can tell the unambiguous single-loser case from the ambiguous one.
        The new ID is recomputed from the current on-disk maximum each call,
        so several renumbers in one reconcile each get a distinct fresh ID.

        A file whose body is not an issue mapping (corrupt / hand-edited
        YAML) was still counted into its collision group — via the filename
        prefix — so skipping it would leave the duplicate ID in place
        unrepaired. Its filename IS its numeric identity in that case, so a
        rename alone restores uniqueness; the body is left byte-identical
        (there is no ``id`` field to rewrite) and the skipped in-file rewrite
        is surfaced as a WARNING.

        The replacement number is reserved through the SHARED
        :func:`reserve_next_id` primitive — the same fcntl-locked allocator
        ``se3 issue create`` / ``adopt_issue`` use — rather than a bare
        working-tree ``max(ID)+1`` scan. None of the concurrent creators
        contend on the merge lock, so scanning the max alone could re-mint a
        number a concurrent ``se3 issue create`` had just reserved (its counter
        bump lands before its ``N_*.yaml`` file exists). Reserving under the
        counter lock both honours that reservation and advances the counter, so
        each renumber in a reconcile also gets a distinct fresh number without
        depending on the prior renumbered file already being on disk.

        Returns:
            ``(record, new_path)``.
        """
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            data = None

        old_id = f"{old_num:03d}"
        # The identity is the parsed id, so the filename need not carry a
        # numeric prefix; reuse its stem as the slug when it does not.
        stem_match = re.match(r"^\d+_(.*)$", path.stem)
        slug = stem_match.group(1) if stem_match else path.stem

        new_id = f"{reserve_next_id(self.project_root):03d}"
        new_path = path.parent / f"{new_id}_{slug}.yaml"
        if isinstance(data, dict):
            data["id"] = new_id
            new_path.write_text(self._dump_issue_yaml(data), encoding="utf-8")
            path.unlink()
        else:
            path.rename(new_path)
            self._log(
                f"Issue-ID reconciliation: '{self._issue_relpath(path)}' does "
                f"not parse to an issue mapping; renamed to "
                f"'{new_path.name}' to restore ID uniqueness, but its body "
                f"carries no rewritable 'id' field or renumber trace.",
                level=logging.WARNING,
            )

        status_dir = "closed" if path.parent.name == "closed" else "open"
        record = IssueMergeRecord(
            old_id=old_id, new_id=new_id, status_dir=status_dir,
        )
        return record, new_path

    def _classify_merge_scope(
        self, pre_merge_sha: str, created_paths: list[Path],
    ) -> tuple[list[Path], list[tuple[Path, str]]]:
        """Split the issue store by what the merge wrote, against *pre_merge_sha*.

        Returns ``(incoming_files, merge_modified)``:
          * *incoming_files* — files the merge INTRODUCED (present at HEAD,
            absent at *pre_merge_sha*); every line in them was authored by the
            merged branch. The renamed collision losers *created_paths* this
            reconcile produced are included too: their content is merge-authored
            but lives at a fresh path absent from HEAD, so the tree diff alone
            would miss them. A loser's original HEAD path is dropped here — it
            was renamed away and no longer exists on disk.
          * *merge_modified* — ``(path, baseline_text)`` for files that existed
            at *pre_merge_sha* but whose committed content the merge changed;
            only their merge-ADDED lines belong to the branch.

        Membership is read from the committed git trees, never the working
        directory: a working-tree scan would classify the user's untracked
        drafts as incoming and their uncommitted edits as merge-modified, then
        rewrite their references or stage them into the fix-up commit.
        """
        added, modified = self._merge_authored_issue_paths(pre_merge_sha)
        incoming_files: list[Path] = []
        for relpath in sorted(added):
            path = self.project_root / relpath
            # A merge-introduced file renamed away by phase 1 no longer exists
            # at its committed path; its content now lives at the renamed new
            # path, appended from created_paths below.
            if path.exists():
                incoming_files.append(path)
        incoming_files.extend(created_paths)

        merge_modified: list[tuple[Path, str]] = []
        for relpath in sorted(modified):
            path = self.project_root / relpath
            if not path.exists():
                continue
            baseline = _read_file_from_ref(
                self.project_root, relpath, pre_merge_sha,
            )
            if baseline is None:
                continue
            merge_modified.append((path, baseline))
        return incoming_files, merge_modified

    def _append_renumber_trace_to_file(
        self, path: Path, old_id: str, new_id: str,
    ) -> None:
        """Append the old->new trace to *path*'s description tail.

        Appended at the tail so it never shifts the first non-empty
        description line (which display_title / slug derive from). A file
        that does not parse to an issue mapping is left untouched — there is
        no description to carry the trace, and clobbering unknown content
        would be worse than the missing line (the renumber itself was already
        surfaced as a WARNING and a report record).
        """
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            return
        if not isinstance(data, dict):
            return
        trace = format_renumber_trace(old_id, new_id)
        desc = str(data.get("description", "") or "")
        data["description"] = (
            desc.rstrip() + "\n\n" + trace if desc.strip() else trace
        )
        path.write_text(self._dump_issue_yaml(data), encoding="utf-8")

    def _commit_issue_reconciliation(
        self,
        branch: str,
        records: list[IssueMergeRecord],
        created_paths: list[Path],
        restore_relpaths: set[str],
    ) -> bool:
        """Stage the reconcile's touched paths and commit them as a fix-up commit.

        Stages ONLY the exact paths this reconcile wrote — the renamed losers,
        the files whose references it rewrote, and ``.next_id`` — never
        ``git add -A -- se3/issues``. A blanket add would sweep pre-existing
        untracked issue files (issues the user is still drafting) into the
        renumber fix-up commit, silently publishing unrelated work.
        """
        created_relpaths = {self._issue_relpath(p) for p in created_paths}
        stage_relpaths = sorted(restore_relpaths | created_relpaths)
        if not stage_relpaths:
            # Nothing to stage means nothing was renumbered; treat as a no-op
            # success rather than running a pathspec-less ``git add -A`` that
            # would stage the entire tree.
            return True
        add = _run_git(
            self.project_root, "add", "-A", "--", *stage_relpaths,
            check=False, timeout=30,
        )
        if add.returncode != 0:
            self._log(
                f"Failed to stage renumbered issues for '{branch}': "
                f"{redact_text(add.stderr.strip())}",
                level=logging.ERROR,
            )
            return False
        summary = ", ".join(f"#{r.old_id}->#{r.new_id}" for r in records)
        commit = _run_git(
            self.project_root, "commit", "--no-edit",
            "-m",
            f"se3 merge: renumber colliding issue ID(s) after merging "
            f"'{branch}' ({summary})",
            check=False, timeout=30,
        )
        if commit.returncode != 0:
            self._log(
                f"Failed to commit issue reconciliation for '{branch}': "
                f"{redact_text(commit.stderr.strip())}",
                level=logging.ERROR,
            )
            return False
        return True

    def _restore_issues_worktree(
        self, created_paths: list[Path], restore_relpaths: set[str],
    ) -> None:
        """Undo THIS reconcile's issue-store writes without disturbing anything else.

        Reconciliation runs on top of the already-committed merge, so an abort
        (fix-up commit failed, or an exception escaped) must roll back ONLY the
        paths this run wrote. The old blanket ``git reset``/``git checkout``/
        ``git clean -fdq`` over ``se3/issues`` was destructive: ``git clean``
        irrecoverably deletes EVERY untracked file under ``se3/issues`` —
        including pre-existing uncommitted issue YAMLs the reconciliation never
        touched — and ``git checkout`` discards unrelated uncommitted edits to
        tracked issue files. That is the very "never lose an issue" guarantee
        this machinery exists to uphold, so the rollback is now surgical:
        created files (the renamed losers, absent at HEAD, which ``git
        checkout`` cannot restore-by-removal) are unlinked directly, and every
        tracked path this run modified or renamed away from is restored to HEAD
        one exact path at a time. Confined to ``se3/issues`` throughout.
        """
        created_relpaths = {self._issue_relpath(p) for p in created_paths}
        # Unstage exactly the paths this reconcile wrote (a prior failed fix-up
        # commit's ``git add`` may have staged them). Scoping to the explicit
        # list keeps unrelated untracked/edited issue files out of it.
        stage_relpaths = sorted(restore_relpaths | created_relpaths)
        if stage_relpaths:
            try:
                _run_git(
                    self.project_root, "reset", "-q", "HEAD", "--",
                    *stage_relpaths, check=False, timeout=15,
                )
            except subprocess.TimeoutExpired as exc:
                self._log(
                    f"Timed out unstaging se3/issues during reconcile "
                    f"rollback: {exc}",
                    level=logging.ERROR,
                )
        # Delete the renumbered-loser files this run created; they are absent
        # at HEAD, so no ``git checkout`` can restore-by-removal.
        for path in created_paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                self._log(
                    f"Could not remove reconcile artifact "
                    f"'{self._issue_relpath(path)}': {exc}",
                    level=logging.ERROR,
                )
        # Restore each tracked path individually so one path missing from HEAD
        # (e.g. a not-yet-committed ``.next_id``) cannot abort the whole batch
        # and leave the others un-restored. Created paths are excluded — they
        # were just unlinked and do not exist at HEAD. ``.next_id`` is ALSO
        # excluded: the counter is strictly monotonic and must never move
        # backwards. Checking it out to HEAD (the merge commit's committed
        # value) would erase any advance made since — including a live
        # reservation written by a concurrent allocator that does not hold the
        # merge lock — and the next allocation would re-mint that reserved
        # number, producing the very duplicate ID this machinery prevents.
        # Its advanced value is harmless (number-range holes are fine); leaving
        # it in place is the safe rollback. Unstaging it above is still correct
        # — that only clears the failed fix-up's index entry, not the value.
        for relpath in sorted(
            restore_relpaths - created_relpaths - {"se3/issues/.next_id"}
        ):
            try:
                _run_git(
                    self.project_root, "checkout", "HEAD", "--", relpath,
                    check=False, timeout=15,
                )
            except subprocess.TimeoutExpired as exc:
                self._log(
                    f"Timed out restoring '{relpath}' during reconcile "
                    f"rollback: {exc}",
                    level=logging.ERROR,
                )

    def _reconcile_committed_issue_ids(
        self, branch: str, pre_merge_sha: str, report: MergeReport,
    ) -> Optional[str]:
        """Renumber committed issues that collide on a numeric ID after merge.

        A clean ``git merge`` can bring in an issue file whose numeric ID
        (the record's parsed ``id`` field, falling back to the ``NNN_slug.yaml``
        filename prefix) already names a *different* issue on the current
        branch. Left alone that is a silent duplicate ID. Detection and repair
        are confined to
        ``se3/issues/{open,closed}`` (the issue files plus the ``.next_id``
        counter) and never touch other ``se3/`` runtime state.

        The side that already existed at *pre_merge_sha* is kept; the
        merge-introduced side is renumbered to ``max(ID)+1`` via the shared G1
        primitives (rename, old->new trace, cross-reference rewrite, advance
        ``.next_id``), then staged and committed as an independent fix-up on
        top of the merge commit. When ``#old`` could have named more than one
        issue from the merged branch's perspective — several renumbered
        losers, a merge-introduced keeper that kept the number, or a
        pre-existing keeper that was ALSO part of the branch's own tree
        (it predated the fork) — a remaining ``#old``
        reference has no provable target;
        it is left un-rewritten and the ambiguity is recorded durably — a
        note appended to each affected issue plus a
        ``MergeReport.ambiguous_issue_references`` entry — never silently
        repointed at a guess.

        Best-effort: any failure is logged, the working tree is restored to
        the merge commit's ``se3/issues`` state, and the already-successful
        git merge is never rolled back. Always returns ``None`` — the merge's
        success never depends on reconciliation.
        """
        self._last_branch_reconcile_left_fixup = False
        issues_root = self.project_root / "se3" / "issues"
        if not issues_root.exists():
            return None

        # Exactly what THIS reconcile writes, so both the fix-up commit's
        # staging and any rollback touch only these paths — never a blanket
        # sweep of se3/issues, which would delete pre-existing untracked issue
        # files or discard unrelated uncommitted edits. ``created_paths`` are
        # the renamed losers (absent at HEAD, removed by unlink on rollback);
        # ``restore_relpaths`` are tracked paths modified or renamed away from
        # (restored to HEAD). ``.next_id`` is always included — it is advanced.
        created_paths: list[Path] = []
        restore_relpaths: set[str] = {"se3/issues/.next_id"}

        try:
            # Authorship for BOTH detection and reference-scope classification
            # is read from the committed merge commit, never the working tree,
            # so untracked drafts / uncommitted edits stay out of this channel.
            head_tracked = self._issue_relpaths_at_ref("HEAD")
            collisions = self._detect_committed_id_collisions(
                issues_root, "HEAD", head_tracked,
            )
            if not collisions:
                return None

            # Phase 1 — rename + re-id every merge-introduced colliding file.
            # References are NOT rewritten yet: only once every loser holds
            # its final ID is "which #old token means which issue" decidable.
            renumbered: list[tuple[IssueMergeRecord, Path, int]] = []
            # Old IDs whose KEEPER is itself merge-introduced (no colliding
            # copy existed at pre_merge_sha, so the lexicographic fallback
            # picked one of the branch's own files). The branch then shipped
            # SEVERAL issues under that number even when only one loser was
            # renumbered — a remaining ``#old`` may equally mean the keeper
            # (which still holds it), so the group is ambiguous regardless of
            # the loser count.
            incoming_keeper_old: set[int] = set()
            # Old IDs whose PRE-EXISTING keeper was also part of the merged
            # branch's own tree (it predated the fork). The branch's store
            # then held two issues under that number, so a branch-authored
            # ``#old`` could mean the inherited keeper just as well as the
            # branch's own (renumbered) file — the group stays ambiguous
            # even with a single loser.
            keeper_on_branch_old: set[int] = set()
            for numeric_id, paths in collisions:
                keep = self._choose_kept_issue(paths, pre_merge_sha)
                if not self._path_in_ref(pre_merge_sha, keep):
                    incoming_keeper_old.add(numeric_id)
                elif self._keeper_lived_on_branch(branch, keep):
                    keeper_on_branch_old.add(numeric_id)
                for path in paths:
                    if path == keep:
                        continue
                    # The loser's HEAD-committed path is renamed away — record
                    # it so a rollback restores it (its content lives at HEAD).
                    restore_relpaths.add(self._issue_relpath(path))
                    result = self._renumber_committed_issue(path, numeric_id)
                    renumbered.append((*result, numeric_id))
                    created_paths.append(result[1])

            if not renumbered:
                return None
            records = [record for record, _path, _num in renumbered]

            incoming_files, merge_modified = self._classify_merge_scope(
                pre_merge_sha, created_paths,
            )

            # Tracked files the reconcile may rewrite (cross-references,
            # ambiguity notes): merge-introduced files still committed at HEAD
            # and pre-existing files the merge edited. The renamed losers this
            # run created are absent at HEAD, so they are excluded here and
            # rolled back by unlink instead of checkout.
            created_relpaths = {self._issue_relpath(p) for p in created_paths}
            for f in incoming_files:
                rp = self._issue_relpath(f)
                if rp not in created_relpaths:
                    restore_relpaths.add(rp)
            for f, _baseline in merge_modified:
                restore_relpaths.add(self._issue_relpath(f))

            new_ids_by_old: dict[int, list[str]] = {}
            for record, _new_path, old_num in renumbered:
                new_ids_by_old.setdefault(old_num, []).append(record.new_id)

            # Phase 2a — a renumbered file's OWN ``#old`` tokens are provably
            # self-references only when the old ID named a SINGLE issue in
            # the branch's store: one loser, no merge-introduced keeper, and
            # no inherited pre-fork keeper. Otherwise a ``#old`` inside a
            # loser could equally mean a colliding peer or the keeper, so
            # forcing it to the file's own new ID would silently turn a peer
            # reference into a self-reference — those files are left for the
            # ambiguity pass below instead.
            for record, new_path, old_num in renumbered:
                if (
                    len(new_ids_by_old[old_num]) == 1
                    and old_num not in incoming_keeper_old
                    and old_num not in keeper_on_branch_old
                ):
                    rewrite_issue_references(
                        self.project_root, old_num, record.new_id,
                        scope_files=[new_path],
                    )

            # Phase 2b — the branch's remaining ``#old`` references (in other
            # merge-introduced files, and in merge-added lines of pre-existing
            # files it edited). Unambiguous only when ``#old`` named exactly
            # ONE issue in the branch's own store: a single renumbered loser
            # and no keeper the branch could see. Otherwise the branch's
            # store was already ambiguous — no context can prove which
            # issue a remaining ``#old`` meant (this includes the
            # renumbered files' own bodies), and rewriting it to a guess would
            # silently corrupt the reference. Those tokens stay in place, but
            # each affected file is found and the ambiguity is recorded
            # durably (a note in the file + a report entry) instead of only
            # leaving them pointing at the keeper unannounced.
            ambiguous_refs: list[tuple[Path, int, list[str]]] = []
            for old_num, new_ids in new_ids_by_old.items():
                # A keeper the branch could see is a live candidate target
                # too — whether merge-introduced (it kept ``#old`` itself) or
                # pre-existing but inherited by the branch before the fork.
                # Either way it joins the renumbered peers on the candidate
                # list and forces the group ambiguous even with a single
                # loser.
                candidates = (
                    [f"{old_num:03d}"]
                    if (
                        old_num in incoming_keeper_old
                        or old_num in keeper_on_branch_old
                    )
                    else []
                ) + list(new_ids)
                if len(candidates) > 1:
                    for f in incoming_files:
                        if f.exists() and live_reference_count(f, old_num):
                            ambiguous_refs.append((f, old_num, candidates))
                    for f, baseline in merge_modified:
                        if rewrite_references_in_added_lines(
                            f, baseline, old_num, old_num, dry_run=True,
                        ):
                            ambiguous_refs.append((f, old_num, candidates))
                    self._log(
                        f"Issue-ID reconciliation: old ID #{old_num:03d} "
                        f"named more than one issue from the merged branch's "
                        f"perspective; remaining "
                        f"#{old_num:03d} references are ambiguous "
                        f"(candidates: "
                        f"{', '.join('#' + n for n in candidates)}) and were "
                        f"left un-rewritten; the ambiguity is recorded in "
                        f"the affected issue(s) and the merge report.",
                        level=logging.WARNING,
                    )
                    continue
                rewrite_issue_references(
                    self.project_root, old_num, new_ids[0],
                    scope_files=incoming_files,
                )
                for f, baseline in merge_modified:
                    rewrite_references_in_added_lines(
                        f, baseline, old_num, new_ids[0],
                    )

            # Phase 3 — traces and ambiguity notes last, after every rewrite
            # pass, so a pass can never repoint their embedded ``#old``.
            for record, new_path, _old_num in renumbered:
                self._append_renumber_trace_to_file(
                    new_path, record.old_id, record.new_id,
                )
            ambiguity_entries: list[dict] = []
            for f, old_num, new_ids in ambiguous_refs:
                append_description_note(
                    f, format_ambiguous_reference_note(old_num, new_ids),
                )
                ambiguity_entries.append({
                    "file": self._issue_relpath(f),
                    "old_id": f"{old_num:03d}",
                    "candidates": list(new_ids),
                })

            # Push .next_id to the new global max so no future allocation
            # re-collides with a number we just assigned.
            advance_next_id_to_max(self.project_root)

            if not self._commit_issue_reconciliation(
                branch, records, created_paths, restore_relpaths,
            ):
                self._restore_issues_worktree(created_paths, restore_relpaths)
                return None

            report.committed_issue_renumbers.extend(records)
            report.ambiguous_issue_references.extend(ambiguity_entries)
            self._last_branch_reconcile_left_fixup = True
            for rec in records:
                self._log(
                    f"Reconciled colliding committed issue ID for "
                    f"'{branch}': #{rec.old_id} -> #{rec.new_id}"
                )
        except Exception as exc:
            # Best-effort contract: never let a reconciliation problem roll
            # back an already-successful git merge.
            self._log(
                f"Issue-ID reconciliation for '{branch}' failed: {exc}. "
                f"The git merge is intact; issue store restored.",
                level=logging.ERROR,
                exc_info=True,
            )
            self._restore_issues_worktree(created_paths, restore_relpaths)
            return None
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
                timeout=self._git_merge_timeout,
            )
        except subprocess.TimeoutExpired:
            self._log(f"git merge timed out for branch '{branch}'")
            abort_ok = self._abort_merge()
            if self.strategy == MergeStrategy.FAST:
                if not abort_ok:
                    report.failure_reason = FailureReason.MERGE_ABORT_FAILED.legacy_string
                else:
                    report.failure_reason = FailureReason.MERGE_TIMED_OUT.legacy_string
                return "fast_abort"
            if not abort_ok:
                report.failure_reason = FailureReason.MERGE_ABORT_FAILED.legacy_string
            else:
                report.failure_reason = FailureReason.MERGE_TIMED_OUT.legacy_string
            return "non_conflict_failure"

        if result.returncode == 0:
            # Clean merge — run guardrails on spec files
            rev_parse_result = None
            try:
                rev_parse_result = _run_git(
                    self.project_root, "rev-parse", "HEAD",
                    check=False, timeout=15,
                )
                # G3 fix: a non-zero returncode produces an empty
                # ``stdout.strip()`` that masquerades as a successful
                # SHA read. Fail loudly rather than letting an empty
                # SHA flow into the post-condition checks below.
                if rev_parse_result.returncode != 0:
                    self._log(
                        f"git rev-parse HEAD failed (rc="
                        f"{rev_parse_result.returncode}, stderr="
                        f"{rev_parse_result.stderr.strip()}) after "
                        f"clean merge of '{branch}'. Cannot verify "
                        f"merge commit SHA; treating as failure."
                    )
                    post_merge_sha = ""
                else:
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
                # B1 post-condition: ancestry holds even though no merge
                # commit was produced. This catches the silent-loss case
                # where some intermediate step lost the branch reference.
                pc_result = self._verify_post_merge_conditions(
                    branch, already_ancestor=True, report=report,
                )
                if pc_result is not None:
                    return pc_result
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
                report.human_call_file = exc.call_file
                report.pending_human = True
                report.failure_reason = exc.failure_reason.legacy_string
                self._last_stall_iteration_count = exc.iteration_count
                # Return the legacy string so the dispatch loop in execute()
                # can match the existing string-keyed elif branches
                # (e.g. "guardrail_repair_stalled", "guardrail_repair_exhausted").
                # Returning the raw FailureReason enum would fall through to
                # the catch-all "unexpected result" else branch and produce a
                # misleading log message for operators.
                return exc.failure_reason.legacy_string
            except GuardrailRepairFailed as exc:
                if exc.failure_reason is FailureReason.GUARDRAIL_CHECK_FAILED:
                    self._log(
                        f"Guardrails check itself crashed for '{branch}' in fast mode: {exc}"
                    )
                elif exc.failure_reason is FailureReason.GUARDRAIL_REPAIR_STALLED_CALL_FAILED:
                    self._log(
                        f"Guardrail repair stalled for '{branch}' in fast mode: "
                        f"rollback succeeded but the stalled human call file could not be written. {exc}"
                    )
                elif exc.failure_reason is FailureReason.GUARDRAIL_REPAIR_EXHAUSTED_CALL_FAILED:
                    self._log(
                        f"Guardrail repair exhausted for '{branch}' in fast mode: "
                        f"rollback succeeded but the exhausted human call file could not be written. {exc}"
                    )
                else:
                    self._log(
                        f"Guardrail repair failed for '{branch}' in fast mode: {exc}"
                    )
                report.rollback_failed = getattr(exc, "rollback_failed", False)
                report.failure_reason = exc.failure_reason.legacy_string
                return "fast_abort"
            except GuardrailCallFileError as exc:
                self._log(
                    f"Guardrail violation detected, rollback succeeded, but "
                    f"call file could not be written: {exc}"
                )
                report.rollback_failed = False
                report.failure_reason = FailureReason.GUARDRAIL_VIOLATION_CALL_FAILED.legacy_string
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
            except GuardrailRepairInconsistentState as exc:
                # A1-A4 safety contract: the repairer created a commit but
                # could not capture pre_repair_sha, so rollback was refused.
                # HEAD still contains the repair commit but the working tree
                # was restored.  This is a hard-stop: subsequent branches must
                # NOT run because the repo is in an inconsistent state.
                self._log(
                    f"CRITICAL: Guardrail repair for '{branch}' entered an "
                    f"inconsistent state. {exc}",
                    level=logging.ERROR,
                )
                report.failure_reason = FailureReason.INCONSISTENT_REPAIR_STATE.legacy_string
                report.failure_detail = str(exc)
                return "inconsistent_repair_state"
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
                rev_parse_refresh = _run_git(
                    self.project_root, "rev-parse", "HEAD",
                    check=False, timeout=15,
                )
                # G3 fix: a non-zero returncode produces an empty
                # ``stdout.strip()`` that masquerades as a successful
                # SHA read.  Surface a warning rather than letting an
                # empty SHA flow downstream as if it were valid.
                if rev_parse_refresh.returncode != 0:
                    self._log(
                        f"git rev-parse HEAD failed (rc="
                        f"{rev_parse_refresh.returncode}, stderr="
                        f"{rev_parse_refresh.stderr.strip()}) after "
                        f"guardrails check for '{branch}'. Downstream "
                        f"SHA may be stale."
                    )
                    # Keep the old post_merge_sha rather than clobber to ""
                else:
                    refreshed_sha = rev_parse_refresh.stdout.strip()
                    if refreshed_sha:
                        post_merge_sha = refreshed_sha
            except subprocess.TimeoutExpired:
                self._log(
                    f"git rev-parse HEAD timed out after guardrails check for '{branch}'. "
                    "Downstream SHA may be stale."
                )
                # Keep the old post_merge_sha (or empty if already unset)
            # B1 post-condition: confirm the merge commit is still on HEAD
            # before declaring success. Catches silent-loss bugs where a
            # later step (rollback, future amend logic) drops the merge
            # commit between commit-time and report-time.
            #
            # Fix-up tolerance is opt-in to fast-mode only, where
            # GuardrailRepairer may have placed a fix-up commit on top of
            # the merge commit. For default/strict, HEAD MUST itself be
            # the merge commit.
            pc_result = self._verify_post_merge_conditions(
                branch,
                already_ancestor=False,
                report=report,
                allow_fixup_parent=(
                    self.strategy == MergeStrategy.FAST
                    and self._last_branch_repair_ran
                    and not self._last_branch_repair_used_amend
                ),
            )
            if pc_result is not None:
                return pc_result
            # Reconcile committed-issue ID collisions this merge introduced
            # (two distinct issue files parsing to one numeric ID) before
            # runtime-sync folds in uncommitted worktree issues. Runs after
            # the post-condition check so it appends its fix-up commit on top
            # of an already-verified merge commit. Best-effort: it never
            # fails the merge.
            self._reconcile_committed_issue_ids(branch, pre_merge_sha, report)
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

        # Non-conflict failure — log stderr (redacted), abort and report
        stderr_msg = result.stderr.strip()
        if stderr_msg:
            self._log(f"git merge stderr: {redact_text(stderr_msg)}")
        if self.strategy == MergeStrategy.FAST:
            if not self._abort_merge():
                self._log(
                    "WARNING: git merge --abort failed — working tree may still be mid-merge"
                )
                report.failure_reason = FailureReason.MERGE_ABORT_FAILED.legacy_string
            else:
                # Use 'fast_failure:' (not 'fast_abort:') for non-conflict
                # git failures so the CLI can distinguish them from conflict-
                # resolution aborts and produce an accurate message.
                report.failure_reason = (
                    f"{FailureReason.FAST_FAILURE.legacy_string}: {stderr_msg}"
                    if stderr_msg
                    else FailureReason.FAST_FAILURE.legacy_string
                )
                # Also surface the raw git error as structured detail, not only
                # embedded in the compound reason string, so consumers reading
                # failure_detail (e.g. a merge git refused to START, where
                # _abort_merge now succeeds and leaves the real cause here) see
                # the diagnostic instead of a null.
                if stderr_msg:
                    report.failure_detail = stderr_msg
            return "fast_abort"
        if not self._abort_merge():
            self._log(
                "WARNING: git merge --abort failed — working tree may still be mid-merge"
            )
            report.failure_reason = FailureReason.MERGE_ABORT_FAILED.legacy_string
        else:
            report.failure_reason = (
                f"{FailureReason.MERGE_FAILED.legacy_string}: {stderr_msg}"
                if stderr_msg
                else FailureReason.MERGE_FAILED.legacy_string
            )
            # Mirror the raw git error into structured detail (see the FAST
            # branch above) so a merge that git refused to START — where
            # _abort_merge now reports success — preserves its real cause in
            # failure_detail rather than leaving it null.
            if stderr_msg:
                report.failure_detail = stderr_msg
        return "non_conflict_failure"

    def _resolve_deterministic_conflicts(self) -> Optional[DeterministicOutcome]:
        """Mechanically resolve and stage the conflicts a resolver owns.

        Returns ``None`` when the pass could not run at all.  This layer is an
        optimisation in front of the LLM path, never a precondition for it: any
        escaping exception degrades to "resolve nothing deterministically",
        which is precisely the behaviour that existed before it. A bug here
        must not be able to fail a merge that would otherwise have succeeded.
        """
        try:
            conflict_paths = get_conflicting_files(self.project_root)
            outcome = resolve_deterministic(self.project_root, conflict_paths)
        except Exception as exc:
            self._log(
                f"Deterministic conflict pass failed ({exc}) — "
                f"every conflict falls back to the LLM"
            )
            return None

        for path in outcome.resolved:
            resolver = _find_resolver(path)
            name = resolver.name if resolver is not None else "?"
            self._log(f"Deterministically resolved: {path} by {name}")
        for path, reason in outcome.failures.items():
            self._log(
                f"Deterministic resolver failed for {path} ({reason}) — "
                f"falling back to LLM"
            )
        return outcome

    def _handle_conflict(
        self,
        branch: str,
        pre_merge_sha: str,
        report: MergeReport,
    ) -> str:
        """Handle a merge conflict with LLM resolution.

        Returns:
            "merged" (if resolved and committed), "pending_human",
            "guardrail_violation", "fast_abort", "context_build_failed"
            (when build_conflict_context itself raised, indicating the
            merge was aborted because we could not even prepare the
            resolver input — distinct from a real conflict-resolution
            rejection), or "conflict" (if rejected/aborted).
        """
        # Settle the mechanically-resolvable conflicts before any context is
        # built.  Two reasons this runs first rather than filtering
        # ``context.files`` afterwards: the LLM must never see these files
        # (a regenerated 2.5MB code-index alone yielded a ~10M-char editor
        # prompt that every agent rejects), and building their context means
        # reading four multi-megabyte copies we would then throw away.
        det_outcome = self._resolve_deterministic_conflicts()
        ours_branch = getattr(self, "_current_branch", "HEAD")

        # --- Nothing left for a human or an LLM to judge ---
        # Every conflicting path had a mechanical merge rule, so the index is
        # already fully staged.  Returning before ``build_conflict_context``
        # matters: that call collects merge metadata (logs, base sha, per-file
        # stage contents) whose failure would abort a merge that has, in fact,
        # nothing left to inspect.  The context handed to ``_apply_resolution``
        # is therefore synthesised, not built.  This short-circuits STRICT too:
        # its contract is that *contended content* gets human review, and a
        # regenerated index or a monotonic counter carries no decision to review.
        if det_outcome is not None and det_outcome.resolved and not det_outcome.remaining:
            self._log(
                f"All {len(det_outcome.resolved)} conflict(s) resolved "
                f"deterministically — committing merge without LLM"
            )
            from .conflict_context import ConflictContext
            from .conflict_resolver import Confidence, LLMResolution
            return self._apply_resolution(
                branch,
                LLMResolution(
                    files=[],
                    overall_confidence=Confidence.HIGH,
                    flags={"llm_invoked": False, "deterministic": True},
                ),
                pre_merge_sha,
                ConflictContext(
                    project_root=self.project_root,
                    ours_branch=ours_branch,
                    theirs_branch=branch,
                ),
                report,
            )

        # Build conflict context (must be called while mid-merge).
        # Narrowed from ``except Exception`` to a typed error set so
        # programming bugs (TypeError/AttributeError) crash loudly
        # instead of being relabeled ``context_build_failed`` —
        # consistent with the project's "no silent except-Exception"
        # rule applied elsewhere in this module.  Each entry covers a
        # plausible failure that ``build_conflict_context`` may raise
        # in the wild:
        #   * subprocess.SubprocessError — git invocation problems,
        #   * OSError — filesystem read/permission issues,
        #   * ValueError / RuntimeError — malformed conflict-input
        #     parsing (binary content, partial reads, etc.).
        try:
            context = build_conflict_context(
                self.project_root,
                ours_branch,
                branch,
                # ``None`` (the deterministic pass could not run) lets ``build``
                # enumerate the conflicts itself — i.e. the pre-deterministic
                # behaviour, exactly.
                conflict_files=det_outcome.remaining if det_outcome else None,
            )
        except (
            subprocess.SubprocessError,
            OSError,
            ValueError,
            RuntimeError,
        ) as exc:
            self._log(f"Failed to build conflict context: {exc}")
            abort_ok = self._abort_merge()
            if self.strategy == MergeStrategy.FAST:
                if not abort_ok:
                    report.failure_reason = FailureReason.MERGE_ABORT_FAILED.legacy_string
                else:
                    report.failure_reason = FailureReason.CONFLICT_CONTEXT_FAILED.legacy_string
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
                    report.failure_reason = FailureReason.CONFLICT_CONTEXT_FAILED.legacy_string
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
                        FailureReason.CONFLICT_CONTEXT_FAILED_CALL_FILE_WRITE_FAILED.legacy_string
                    )
                    return "context_build_failed"
            if not abort_ok:
                report.failure_reason = FailureReason.MERGE_ABORT_FAILED.legacy_string
            elif not report.failure_reason:
                report.failure_reason = FailureReason.CONFLICT_CONTEXT_FAILED.legacy_string
            # Return a dedicated dispatch code so the caller can surface
            # "conflict context could not be built" instead of the
            # misleading "branch has conflicts" message.  The git merge
            # itself was aborted because we could not even prepare the
            # resolver input — this is structurally distinct from a real
            # conflict that the resolver rejected.
            return "context_build_failed"

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
                # Defense-in-depth: a malformed conflict context may
                # carry a ``ConflictHunk`` with non-positive line numbers.
                # Construct each ``HunkResolution`` defensively so a
                # ``HunkValidationError`` does not escape the strict
                # placeholder path (which has no outer except).
                from .conflict_resolver import HunkValidationError as _HunkErr
                placeholder_hunks: list[HunkResolution] = []
                for h in cf.hunks:
                    try:
                        placeholder_hunks.append(
                            HunkResolution(
                                start_line=h.start_line,
                                end_line=h.end_line,
                                confidence=Confidence.LOW,
                                reasoning="Strict strategy: LLM resolution skipped",
                            )
                        )
                    except _HunkErr as h_exc:
                        self._log(
                            f"Strict placeholder: hunk in {cf.path} has "
                            f"invalid line numbers "
                            f"(start={h.start_line!r} end={h.end_line!r}): "
                            f"{h_exc} — substituting placeholder hunk (1,1)."
                        )
                        placeholder_hunks.append(
                            HunkResolution(
                                start_line=1,
                                end_line=1,
                                confidence=Confidence.LOW,
                                reasoning=(
                                    "Strict strategy: LLM resolution skipped "
                                    "(original hunk had invalid line numbers)"
                                ),
                            )
                        )
                placeholder_files.append(
                    FileResolution(
                        path=cf.path,
                        resolved_content=strict_resolved,
                        hunks=placeholder_hunks,
                        overall_confidence=Confidence.LOW,
                        flags={"strict_mode_placeholder": True},
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
                    report.failure_reason = FailureReason.MERGE_ABORT_FAILED.legacy_string
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

        # --- SAFE / FAST: call LLM resolver via batch (LLM-as-editor) path ---
        self._log(f"Conflict detected with branch '{branch}', invoking LLM resolution")

        # Build the BatchContext that the new ``resolve_and_decide`` API
        # expects.  The per-file payload is the existing ``context.files``
        # list which carries the three-way base/ours/theirs/working
        # contents already read by ``build_conflict_context``.
        batch_ctx = BatchContext(
            project_root=context.project_root,
            ours_branch=context.ours_branch,
            theirs_branch=context.theirs_branch,
            merge_base=context.merge_base,
            ours_head_sha=context.ours_head_sha,
            ours_head_message=context.ours_head_message,
            theirs_head_sha=context.theirs_head_sha,
            theirs_head_message=context.theirs_head_message,
            ours_log_oneline=list(context.ours_log_oneline),
            theirs_log_oneline=list(context.theirs_log_oneline),
            has_spec_files=context.has_spec_files,
            strategy=self.strategy,
        )
        max_iter = _load_max_conflict_resolve_iterations(self.project_root)

        from ..llm_caller import LLMCallError
        try:
            decision = self._decider.resolve_and_decide(
                self._resolver,
                list(context.files),
                batch_ctx,
                max_iterations=max_iter,
            )
        except (LLMCallError, ValueError, subprocess.TimeoutExpired) as exc:
            self._log(f"LLM resolution failed: {exc}")
            if self.strategy == MergeStrategy.FAST:
                if not self._abort_merge():
                    report.failure_reason = FailureReason.MERGE_ABORT_FAILED.legacy_string
                else:
                    report.failure_reason = FailureReason.LLM_RESOLUTION_FAILED.legacy_string
                return "fast_abort"
            # SAFE strategy: escalate to human call (do NOT abort yet)
            llm_fail_decision = StrategyDecision(
                action=DecisionAction.HUMAN_CALL,
                reason=redact_text(f"LLM resolution system failure: {exc}"),
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
                    report.failure_reason = FailureReason.MERGE_ABORT_FAILED.legacy_string
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

        # Recover the LLMResolution that the decider stashed on the
        # outcome (the new ``_run_resolver`` path attaches it under
        # ``_resolution`` after calling ``ConflictResolver.resolve``).
        # Fall back to synthesising one from the batch outcome if not
        # present (e.g. strict_batch which short-circuits).  The on-disk
        # state is the canonical artefact — this structured view is
        # only used by downstream consumers (human-call writer,
        # guardrails report).
        resolution = None
        if decision.outcome is not None:
            resolution = getattr(decision.outcome, "_resolution", None)
            if resolution is None:
                resolution = self._resolver._synthesize_resolution_from_outcome(
                    decision.outcome, context,
                )
        if resolution is None:
            from .conflict_resolver import Confidence, LLMResolution
            resolution = LLMResolution(
                files=[],
                overall_confidence=Confidence.LOW,
                flags={},
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
                        report.failure_reason = FailureReason.MERGE_ABORT_FAILED.legacy_string
                    else:
                        report.failure_reason = FailureReason.INCOMPLETE_RESOLUTION.legacy_string
                    return "fast_abort"
                incomplete_decision = StrategyDecision(
                    action=DecisionAction.HUMAN_CALL,
                    reason=f"LLM resolution incomplete — {reason_detail}",
                )
                incomplete_options = {
                    "abort": "Abort merge — run `git merge --abort` and stop",
                    "manual": "Resolve manually — edit files, then run `git add . && git commit`",
                }
                # Compute the call-file name via the shared writer helper
                # so we benefit from the atomic seq counter + sha8 entropy
                # used elsewhere in HumanCallWriter (defect F1 fix).
                # Building the filename locally with only a microsecond
                # timestamp + sanitised branch name re-introduces F1:
                # parallel invocations within the same microsecond would
                # collide.
                from .human_call import _generate_call_filename

                call_file_name = _generate_call_filename(
                    "merge", context.theirs_branch,
                )
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
                        report.failure_reason = FailureReason.MERGE_ABORT_FAILED.legacy_string
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
                    report.failure_reason = FailureReason.MERGE_ABORT_FAILED.legacy_string
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
                report.failure_reason = FailureReason.MERGE_ABORT_FAILED.legacy_string
            else:
                report.failure_reason = FailureReason.RESOLUTION_REJECTED.legacy_string
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
        binary_file_rejected = False
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
                binary_file_rejected = True
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
                    except OSError as exc:
                        # Narrow from a bare `except Exception` so unexpected
                        # exceptions (programming errors, KeyboardInterrupt
                        # subclasses if any) propagate rather than being
                        # silently swallowed during conflict-resolution
                        # cleanup. A read failure here is a typed validation
                        # failure: the resolution proposed deleting a file
                        # but we could not verify the working-tree state is
                        # safe to delete.
                        self._log(
                            f"Could not read {file_res.path} to verify deletion safety: "
                            f"{exc} — skipping"
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
                if binary_file_rejected:
                    validation_reason = "Binary file conflict requires human review"
                else:
                    validation_reason = "Resolved content failed validation"
                validation_decision = StrategyDecision(
                    action=DecisionAction.HUMAN_CALL,
                    reason=validation_reason,
                )
                # G3 fix: surface the human-call write failure
                # explicitly so the abort path does not silently mask
                # a missing call file. Without this flag, an operator
                # would see a non-failure terminal state while the
                # call file is missing, leaving them with no signal
                # to investigate.
                human_call_write_failed = False
                human_call_write_error: Optional[str] = None
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
                    human_call_write_failed = True
                    human_call_write_error = str(exc)
            else:
                human_call_write_failed = False
                human_call_write_error = None
            self._log("Aborting merge due to validation failures")
            abort_ok = self._abort_merge()
            if not abort_ok:
                report.failure_reason = FailureReason.MERGE_ABORT_FAILED.legacy_string
                if self.strategy == MergeStrategy.FAST:
                    return "fast_abort"
                return "merge_abort_failed"
            # G3 fix: when the human-call file write failed but
            # abort succeeded, surface the call-file failure as the
            # primary failure category. Operators need to know that
            # the call file is missing while seeing a non-failure
            # terminal state — masking the failure as a generic
            # validation reason hides the missing-call-file condition.
            if (
                human_call_write_failed
                and self.strategy != MergeStrategy.FAST
            ):
                report.failure_reason = FailureReason.HUMAN_CALL_WRITE_FAILED.legacy_string
                if human_call_write_error:
                    report.failure_detail = human_call_write_error
                return "human_call_write_failed"
            # Use a dedicated reason when a binary file was in the conflict set
            # so the CLI can surface a targeted message.
            if binary_file_rejected:
                if self.strategy == MergeStrategy.FAST:
                    report.failure_reason = FailureReason.BINARY_FILE_CONFLICT_FAST_ABORT.legacy_string
                else:
                    report.failure_reason = FailureReason.BINARY_FILE_CONFLICT.legacy_string
            else:
                report.failure_reason = FailureReason.RESOLUTION_VALIDATION_FAILED.legacy_string
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

                # Under the LLM-as-editor model the file on disk is
                # already the LLM's final output (``resolve_batch``
                # operates by editing the working tree directly).  The
                # ``resolved_content`` we hold here is synthesised from
                # disk by ``_synthesize_resolution_from_outcome`` — so
                # this write is a no-op on the happy path.  We retain
                # it as defense-in-depth for two distinct callers that
                # still produce structured resolved_content directly:
                # (1) merge-respond, which reconstructs an
                # ``LLMResolution`` from a human-edited call file, and
                # (2) test infrastructure that mocks
                # ``ConflictResolver.resolve`` and returns an
                # ``LLMResolution`` whose ``resolved_content``
                # represents what the LLM "would have written" without
                # actually performing the write.  In both cases the
                # write here brings disk into alignment with the
                # structured resolution.  In production this code path
                # is idempotent because the synthesiser reads back the
                # very content the LLM just wrote.
                full_path.parent.mkdir(parents=True, exist_ok=True)
                # Atomic write: temp file + fsync + rename so a crash or
                # signal mid-write never leaves a partially-written file.
                _atomic_write_text(full_path, file_res.resolved_content)

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
                    report.failure_reason = FailureReason.MERGE_ABORT_FAILED.legacy_string
                    if self.strategy == MergeStrategy.FAST:
                        return "fast_abort"
                    return "merge_abort_failed"
                report.failure_reason = FailureReason.RESOLUTION_WRITE_FAILED.legacy_string
                if self.strategy == MergeStrategy.FAST:
                    return "fast_abort"
                return "resolution_write_failed"

        except Exception as exc:
            self._log(f"Exception during resolution application: {exc}")
            abort_ok = self._abort_merge()
            if not abort_ok:
                report.failure_reason = FailureReason.MERGE_ABORT_FAILED.legacy_string
                if self.strategy == MergeStrategy.FAST:
                    return "fast_abort"
                return "merge_abort_failed"
            report.failure_reason = FailureReason.RESOLUTION_WRITE_FAILED.legacy_string
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
                report.failure_reason = FailureReason.MERGE_ABORT_FAILED.legacy_string
                if self.strategy == MergeStrategy.FAST:
                    return "fast_abort"
                return "merge_abort_failed"
            report.failure_reason = FailureReason.RESOLUTION_COMMIT_TIMEOUT.legacy_string
            if self.strategy == MergeStrategy.FAST:
                return "fast_abort"
            return "resolution_commit_timeout"
        if commit_result.returncode != 0:
            self._log(f"Merge commit failed: {redact_text(commit_result.stderr.strip())}")
            abort_ok = self._abort_merge()
            if not abort_ok:
                report.failure_reason = FailureReason.MERGE_ABORT_FAILED.legacy_string
                if self.strategy == MergeStrategy.FAST:
                    return "fast_abort"
                return "merge_abort_failed"
            report.failure_reason = FailureReason.RESOLUTION_COMMIT_FAILED.legacy_string
            if self.strategy == MergeStrategy.FAST:
                return "fast_abort"
            return "resolution_commit_failed"

        # Run guardrails
        try:
            rev_parse_for_guardrails = _run_git(
                self.project_root, "rev-parse", "HEAD",
                check=False, timeout=15,
            )
            # G3 fix: a non-zero returncode produces an empty
            # ``stdout.strip()`` that masquerades as a successful
            # SHA read.  Fail closed rather than letting an empty
            # SHA flow into ``_run_guardrails``.
            if rev_parse_for_guardrails.returncode != 0:
                self._log(
                    "git rev-parse HEAD failed (rc=%d, stderr=%s) "
                    "after merge commit for '%s'. Cannot verify "
                    "post-merge SHA; guardrails will fail closed.",
                    rev_parse_for_guardrails.returncode,
                    rev_parse_for_guardrails.stderr.strip(),
                    branch,
                )
                post_merge_sha = ""
            else:
                post_merge_sha = rev_parse_for_guardrails.stdout.strip()
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
            report.human_call_file = exc.call_file
            report.pending_human = True
            report.failure_reason = exc.failure_reason.legacy_string
            self._last_stall_iteration_count = exc.iteration_count
            # Return the legacy string so the dispatch loop in execute()
            # can match the existing string-keyed elif branches
            # (e.g. "guardrail_repair_stalled", "guardrail_repair_exhausted").
            # Returning the raw FailureReason enum would fall through to
            # the catch-all "unexpected result" else branch and produce a
            # misleading log message for operators.
            return exc.failure_reason.legacy_string
        except GuardrailRepairFailed as exc:
            if exc.failure_reason is FailureReason.GUARDRAIL_CHECK_FAILED:
                self._log(
                    f"Guardrails check itself crashed for '{branch}' in fast mode: {exc}"
                )
            elif exc.failure_reason is FailureReason.GUARDRAIL_REPAIR_STALLED_CALL_FAILED:
                self._log(
                    f"Guardrail repair stalled for '{branch}' in fast mode: "
                    f"rollback succeeded but the stalled human call file could not be written. {exc}"
                )
            elif exc.failure_reason is FailureReason.GUARDRAIL_REPAIR_EXHAUSTED_CALL_FAILED:
                self._log(
                    f"Guardrail repair exhausted for '{branch}' in fast mode: "
                    f"rollback succeeded but the exhausted human call file could not be written. {exc}"
                )
            else:
                self._log(
                    f"Guardrail repair failed for '{branch}' in fast mode: {exc}"
                )
            report.rollback_failed = getattr(exc, "rollback_failed", False)
            report.failure_reason = exc.failure_reason.legacy_string
            return "fast_abort"
        except GuardrailCallFileError as exc:
            self._log(
                f"Guardrail violation detected, rollback succeeded, but "
                f"call file could not be written: {exc}"
            )
            report.rollback_failed = False
            report.failure_reason = FailureReason.GUARDRAIL_VIOLATION_CALL_FAILED.legacy_string
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
        except GuardrailRepairInconsistentState as exc:
            # A1-A4 safety contract: the repairer created a commit but
            # could not capture pre_repair_sha, so rollback was refused.
            # HEAD still contains the repair commit but the working tree
            # was restored.  This is a hard-stop: subsequent branches must
            # NOT run because the repo is in an inconsistent state.
            self._log(
                f"CRITICAL: Guardrail repair for '{branch}' entered an "
                f"inconsistent state. {exc}",
                level=logging.ERROR,
            )
            report.failure_reason = FailureReason.INCONSISTENT_REPAIR_STATE.legacy_string
            report.failure_detail = str(exc)
            return "inconsistent_repair_state"
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
            rev_parse_refresh = _run_git(
                self.project_root, "rev-parse", "HEAD",
                check=False, timeout=15,
            )
            # G3 fix: a non-zero returncode produces an empty
            # ``stdout.strip()`` that masquerades as a successful
            # SHA read.  Surface it as a stale-SHA condition so
            # downstream consumers see the placeholder rather than
            # an empty string.
            if rev_parse_refresh.returncode != 0:
                self._log(
                    f"git rev-parse HEAD failed (rc="
                    f"{rev_parse_refresh.returncode}, stderr="
                    f"{rev_parse_refresh.stderr.strip()}) after "
                    f"guardrails for '{branch}'. SHA may be stale."
                )
                sha_fresh = False
                post_merge_sha = "<unavailable — rev-parse failed>"
            else:
                post_merge_sha = rev_parse_refresh.stdout.strip()
                if not post_merge_sha:
                    sha_fresh = False
                    post_merge_sha = "<unavailable — empty stdout>"
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
        # B1 post-condition: confirm the LLM-resolved merge commit is on
        # HEAD with branch ancestry intact. Mirrors the clean-merge path.
        # Fix-up tolerance is opt-in to fast-mode AND only when guardrail
        # repair actually ran (and may have created a fix-up commit). If
        # repair never ran, HEAD must itself be the merge commit so a
        # stray hook-installed commit cannot pass silently.
        pc_result = self._verify_post_merge_conditions(
            branch,
            already_ancestor=False,
            report=report,
            allow_fixup_parent=(
                self.strategy == MergeStrategy.FAST
                and self._last_branch_repair_ran
                and not self._last_branch_repair_used_amend
            ),
        )
        if pc_result is not None:
            return pc_result
        # Two issue files sharing one numeric ID live at DIFFERENT paths, so
        # they merge cleanly even inside an otherwise-conflicted merge — the
        # hard "no duplicate numeric ID" guarantee therefore applies to the
        # LLM-resolved path exactly as to the clean-merge path. Mirrors the
        # clean path's ordering: after the post-condition check (so the
        # fix-up lands on a verified merge commit), before runtime-sync.
        # Best-effort: it never fails the merge.
        self._reconcile_committed_issue_ids(branch, pre_merge_sha, report)
        sync_result = self._sync_runtime(branch, report)
        if sync_result:
            return sync_result
        return "merged"

    def _run_guardrails(
        self,
        pre_sha: str,
        post_sha: str,
        branch: str,
        strategy: MergeStrategy = MergeStrategy.SAFE,
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
                    failure_reason = FailureReason.GUARDRAIL_MISSING_PRE_AND_POST_SHA
                elif not pre_sha:
                    missing_reason = "pre_sha"
                    failure_reason = FailureReason.GUARDRAIL_MISSING_PRE_SHA
                else:
                    missing_reason = "post_sha"
                    failure_reason = FailureReason.GUARDRAIL_MISSING_POST_SHA
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

            # H5: when the guardrails check could not finish enumeration
            # (e.g. an OSError mid-walk dropped one or more spec files),
            # the violations list is NOT authoritative — a real violation
            # in the unreadable file could be missed.  Treat as a fatal
            # escalation regardless of strategy: skip fast-mode LLM repair
            # and route to human call.  The CHECK_INCOMPLETE violation is
            # already present in the list, so the operator can see why
            # the check was incomplete; the LLM, however, has no trustworthy
            # way to repair what was never seen.
            if getattr(gr_report, "incomplete", False):
                self._log(
                    f"Guardrails check for '{branch}' is INCOMPLETE "
                    f"({len(gr_report.violations)} violation(s) including "
                    f"CHECK_INCOMPLETE entries) — escalating to human call "
                    f"because the LLM cannot repair violations that were "
                    f"never enumerated.",
                    level=logging.WARNING,
                )
                # Re-classify under the safe-strategy escalation so the
                # rollback + human call path below executes.
                strategy = MergeStrategy.SAFE

            # H1/H2: topology violations (CHECK_FAILURE with file_path="N/A")
            # cannot be repaired by editing spec files — the LLM has no
            # meaningful file path to write to.  Skip fast-mode LLM repair
            # and route directly to rollback + human call.
            topology_violations = [
                v for v in gr_report.violations
                if v.violation_type == "CHECK_FAILURE" and v.file_path == "N/A"
            ]
            if topology_violations and strategy == MergeStrategy.FAST:
                self._log(
                    f"Guardrails check for '{branch}' contains "
                    f"{len(topology_violations)} topology violation(s) — "
                    f"the LLM cannot fix merge topology by editing spec "
                    f"files. Escalating to human call instead of fast-mode "
                    f"LLM repair.",
                    level=logging.WARNING,
                )
                strategy = MergeStrategy.SAFE

            self._log(
                f"Guardrails detected {len(gr_report.violations)} violation(s) "
                f"for '{branch}' (reason: post-merge guardrails violation)"
            )
            for v in gr_report.violations:
                self._log(f"  [{v.violation_type}] {v.file_path}: {v.message}")

            # --- fast strategy: attempt LLM repair with iteration limit ---
            if strategy == MergeStrategy.FAST:
                # Mark that repair was attempted on this branch so the
                # post-merge condition check accepts a fix-up commit on
                # top of HEAD. Branches that never enter the repair path
                # keep ``_last_branch_repair_ran=False`` and therefore
                # require HEAD itself to be the merge commit, catching
                # stray hook commits as ``silent_merge_loss``.
                self._last_branch_repair_ran = True
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
                #
                # Initialise last_hash with the initial violation-set hash so
                # that even max_iterations=1 can detect a no-op repair (the
                # first repair produces the same hash as the initial report).
                last_hash: Optional[str] = violation_set_hash(current_violations)

                # G3: track a sliding window of recent (hash, count) pairs
                # so an alternating-flap pattern (the LLM oscillates
                # between two equivalent-but-different violation sets,
                # same root cause but with renamed files / reordered
                # messages / cosmetic content shuffles) is escalated
                # to a human call instead of churning the iteration cap
                # without convergence. The window size is intentionally
                # small (N=4) so a genuinely-progressing repair that
                # legitimately revisits a previous state after making
                # forward progress is not falsely classified as stalled.
                #
                # Two heuristics:
                #   1. recent_hashes ring: same hash recurring within the
                #      last N iterations is an oscillation, not progress.
                #   2. recent_counts: when the violation count is
                #      stationary across the window AND no consecutive
                #      stall fired, this is the "different hash, same
                #      count" alternating flap signature.
                _STALL_WINDOW_SIZE = 4
                recent_hashes: list[str] = [last_hash]
                recent_counts: list[int] = [len(current_violations)]

                # Gather original and merged spec contents.
                # original_specs is read once (pre_sha never changes).
                # merged_specs is refreshed each iteration from the current HEAD
                # so that the LLM sees the latest state after any amendments by
                # previous repair rounds.
                #
                # G3 fix (medium): catch ChangedSpecFilesIncomplete so a
                # transient git-tooling failure does not silently
                # downgrade to "no spec files changed" and bypass the
                # repair loop. We surface the failure as a guardrail
                # repair failure with the specific cause preserved.
                from .guardrails import ChangedSpecFilesIncomplete
                try:
                    spec_files = _get_changed_spec_files(
                        self.project_root, pre_sha, post_sha,
                    )
                except ChangedSpecFilesIncomplete as csi_exc:
                    raise GuardrailRepairFailed(
                        f"Cannot enumerate changed spec files for repair: "
                        f"{csi_exc}",
                        failure_reason=FailureReason.GUARDRAIL_REPAIR_FAILED,
                    ) from csi_exc
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

                # Explicit counter mirrors the `iteration` loop variable so
                # the after-loop blocks (max-iterations escalation,
                # exhausted call-file writer) can reference it without
                # relying on Python's leak-of-loop-variable semantics.  If
                # ``_max_repair_iterations`` is ever 0 the for-loop body
                # never executes and ``iteration`` would be UNDEFINED at
                # the after-loop site, raising UnboundLocalError instead
                # of a clean failure.  The loader clamp pins the value
                # to >=1 today, but a future refactor or test
                # monkeypatch could bypass that — this counter makes
                # the after-loop reference robust regardless.
                iteration_completed = 0
                for iteration in range(1, self._max_repair_iterations + 1):
                    iteration_completed = iteration
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

                    # Defensive contract: refresh post_sha from the just-read
                    # HEAD if the previous iteration's repair amended the
                    # commit and the rollback restored HEAD to a different
                    # SHA than our cached `post_sha`.  In the well-behaved
                    # path the repairer always rolls back to the
                    # pre-repair SHA on failure (which equals the original
                    # `post_sha`), but downstream ancestry / topology
                    # checks inside repair_violations would otherwise
                    # operate against a dangling object if the rollback
                    # ever leaves HEAD at a fix-up SHA. Pass the freshest
                    # HEAD available as `post_sha` for the next call so
                    # the contract is explicit rather than implicit.
                    iter_post_sha = current_head if current_head else post_sha

                    repair_result = self._repairer.repair_violations(
                        branch=branch,
                        pre_sha=pre_sha,
                        post_sha=iter_post_sha,
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
                        # Surface partial repair warning at the
                        # orchestrator level so log readers see when an
                        # incomplete LLM response left some files
                        # unaddressed even though the re-check passed
                        # (e.g. side-effect clearance from another file's
                        # repair masked the violation set).
                        if repair_result.skipped_missing_content:
                            self._log(
                                f"WARNING: Guardrail repair for '{branch}' at "
                                f"iteration {iteration} silently skipped "
                                f"{len(repair_result.skipped_missing_content)} "
                                f"file(s) for which the LLM produced no "
                                f"corrected_content: "
                                f"{', '.join(repair_result.skipped_missing_content)}. "
                                f"Re-check passed regardless — the repair may have "
                                f"benefited from side-effect clearance from another "
                                f"file's repair, so the skipped files MAY still be "
                                f"unaddressed.  Operators SHOULD inspect them.",
                                level=logging.WARNING,
                            )
                        self._last_branch_repair_used_amend = (
                            repair_result.used_amend
                        )
                        return None

                    # Repair failed — re-run guardrails to get fresh violations
                    self._log(
                        f"Guardrail repair iteration {iteration} failed: "
                        f"{repair_result.error}"
                    )

                    # After repair failure the repairer may have rolled HEAD
                    # back to ``post_sha``, but we verify against the *current*
                    # HEAD so a future refactor that changes rollback semantics
                    # does not silently compare against a stale ref.
                    try:
                        current_head = _run_git(
                            self.project_root, "rev-parse", "HEAD",
                            check=False, timeout=15,
                        ).stdout.strip()
                    except subprocess.TimeoutExpired:
                        current_head = post_sha
                    try:
                        fresh_report = self._guardrails.check_merge_result(
                            pre_sha, current_head,
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
                                failure_reason=FailureReason.GUARDRAIL_CHECK_FAILED,
                                rollback_failed=True,
                            ) from rbe
                        raise GuardrailRepairFailed(
                            f"Guardrail repair failed at iteration {iteration} "
                            f"and re-check crashed: {exc}",
                            failure_reason=FailureReason.GUARDRAIL_CHECK_FAILED,
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
                        except subprocess.TimeoutExpired as exc:
                            self._log(
                                "ERROR: git rev-parse HEAD timed out during "
                                f"side-effect clearance check at iteration {iteration}. "
                                "Treating as fail-closed: cannot prove HEAD was not "
                                "silently dropped while filesystem is hung.",
                                level=logging.ERROR,
                            )
                            raise GuardrailRepairFailed(
                                f"Guardrail repair side-effect clearance timed out "
                                f"at iteration {iteration}: {exc}",
                                failure_reason=FailureReason.POSTCOND_CHECK_TIMEOUT,
                            ) from exc
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
                        # Even if guardrails coincidentally cleared between
                        # iterations and HEAD has only "moved on top of" the
                        # merge commit, we must still prove the branch is an
                        # ancestor of the new HEAD before accepting success.
                        # Without this fail-closed check, a HEAD drift caused
                        # by the repairer that ALSO unwound the merge commit
                        # would be silently treated as success.
                        try:
                            assert_branch_merged(
                                self.project_root, branch, timeout=15,
                            )
                        except PostConditionViolated as exc:
                            self._log(
                                f"ERROR: side-effect clearance accepted by "
                                f"guardrails but branch '{branch}' is no longer "
                                f"merged into HEAD ({exc}). Failing closed to "
                                f"prevent silent merge loss.",
                                level=logging.ERROR,
                            )
                            raise GuardrailRepairFailed(
                                f"Guardrails passed on re-check at iteration "
                                f"{iteration} but branch '{branch}' is no "
                                f"longer merged into HEAD: {exc}",
                                failure_reason=FailureReason.SILENT_MERGE_LOSS,
                            ) from exc
                        except subprocess.TimeoutExpired as exc:
                            self._log(
                                f"ERROR: assert_branch_merged timed out during "
                                f"side-effect clearance check at iteration "
                                f"{iteration}: {exc}. Treating as fail-closed.",
                                level=logging.ERROR,
                            )
                            raise GuardrailRepairFailed(
                                f"Branch-merge ancestry check timed out at "
                                f"iteration {iteration}: {exc}",
                                failure_reason=FailureReason.POSTCOND_CHECK_TIMEOUT,
                            ) from exc
                        return None

                    current_hash = violation_set_hash(fresh_report.violations)
                    current_count = len(fresh_report.violations)
                    self._log(
                        f"Repair iteration {iteration}: violation hash "
                        f"{current_hash[:8]}... "
                        f"({current_count} violation(s))"
                    )

                    # G3 alternating-flap detection: examine recent
                    # iterations BEFORE the consecutive-hash check below
                    # so a stall that flaps between two equivalent
                    # violation sets is also escalated to a human call.
                    # The recurrence test fires when:
                    #   (1) ``current_hash`` matches a hash from a recent
                    #       iteration (i.e., the LLM oscillates back to a
                    #       previous state instead of progressing), AND
                    #   (2) the iteration count has reached at least 3
                    #       so a single revisit doesn't trigger a false
                    #       positive on a brief detour-then-return path.
                    is_alternating_flap = False
                    if (
                        iteration >= 3
                        and current_hash in recent_hashes
                        and current_hash != last_hash
                    ):
                        # The hash is being revisited but not consecutively
                        # — this is the alternating-flap signature. Treat
                        # it as a stall to surface the situation to a
                        # human reviewer rather than churning the
                        # iteration cap.
                        is_alternating_flap = True
                    # Stable-count flap: every recent iteration produced
                    # the same violation count (different files / messages
                    # but same count). Stable count plus a window full of
                    # distinct hashes signals an alternating pattern with
                    # no convergence.
                    is_stable_count_flap = False
                    if (
                        iteration >= _STALL_WINDOW_SIZE
                        and len(recent_counts) >= _STALL_WINDOW_SIZE
                        and len(set(recent_counts[-_STALL_WINDOW_SIZE:])) == 1
                        and current_count == recent_counts[-1]
                        and current_hash != last_hash
                        and len(set(recent_hashes[-_STALL_WINDOW_SIZE:])) > 1
                    ):
                        is_stable_count_flap = True

                    if (
                        last_hash is not None and current_hash == last_hash
                        or is_alternating_flap
                        or is_stable_count_flap
                    ):
                        # Stalled — either:
                        #   (a) violation set unchanged from previous repair
                        #       iteration (consecutive identical hash, the
                        #       canonical stall signature), OR
                        #   (b) the violation set is alternating between
                        #       equivalent-but-different states (G3 fix:
                        #       same root cause masquerading via renamed
                        #       files, reordered messages, or cosmetic
                        #       content shuffles). Without this branch,
                        #       a flapping repair burns the iteration cap
                        #       and triggers abort-without-human, hiding
                        #       the actual stall from the operator.
                        if is_alternating_flap:
                            stall_kind = "alternating-flap"
                            stall_reason = (
                                f"violation set hash {current_hash[:8]}... "
                                f"recurred within the last "
                                f"{_STALL_WINDOW_SIZE} iteration(s) — "
                                f"alternating between equivalent violation states"
                            )
                        elif is_stable_count_flap:
                            stall_kind = "stable-count-flap"
                            stall_reason = (
                                f"violation count stable at "
                                f"{current_count} across the last "
                                f"{_STALL_WINDOW_SIZE} iteration(s) "
                                f"with distinct hashes — non-converging flap"
                            )
                        else:
                            stall_kind = "consecutive-identical"
                            stall_reason = (
                                f"violation set hash {current_hash[:8]}... "
                                f"unchanged from previous iteration"
                            )
                        self._log(
                            f"Guardrail repair stalled at iteration {iteration} "
                            f"({stall_kind}): {stall_reason}"
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
                                    failure_reason=FailureReason.GUARDRAIL_REPAIR_STALLED_CALL_FAILED,
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
                            # B11 fix: render call_file defensively so a
                            # logic error that left it None never produces
                            # the literal string "None" in operator output.
                            call_file_str = (
                                str(call_file) if call_file is not None
                                else "<unwritten>"
                            )
                            raise GuardrailRollbackError(
                                f"Guardrail repair stalled at iteration {iteration} "
                                f"but rollback failed. The human call file was written at "
                                f"{call_file_str} for diagnostic evidence.",
                                call_file=call_file,
                            ) from rollback_exc

                        raise GuardrailRepairStalled(
                            f"Guardrail repair stalled after {iteration} "
                            f"iteration(s): LLM could not reduce violations",
                            call_file=call_file,
                            iteration_count=iteration,
                            last_violation_hash=current_hash,
                            failure_reason=FailureReason.GUARDRAIL_REPAIR_STALLED,
                        )

                    last_hash = current_hash
                    # G3: maintain bounded recent_hashes / recent_counts
                    # ring buffers for the alternating-flap detector
                    # above. Prepend then truncate so the most recent
                    # entry sits at index -1 and the window is always
                    # at most _STALL_WINDOW_SIZE entries.
                    recent_hashes.append(current_hash)
                    if len(recent_hashes) > _STALL_WINDOW_SIZE:
                        recent_hashes = recent_hashes[-_STALL_WINDOW_SIZE:]
                    recent_counts.append(current_count)
                    if len(recent_counts) > _STALL_WINDOW_SIZE:
                        recent_counts = recent_counts[-_STALL_WINDOW_SIZE:]
                    # Update working violation list for next iteration's repair
                    # prompt. Use a local variable rather than mutating the
                    # original gr_report object so any retained references
                    # elsewhere do not observe stale data.
                    current_violations = fresh_report.violations
                # WARNING: This is a ``for ... else`` pattern. The ``else``
                # runs only when the loop completes all iterations WITHOUT
                # an early ``return``, ``raise``, or ``break``. Adding a
                # ``break`` here would silently skip the exhausted-iterations
                # escalation below. If you need an early exit, use ``return``
                # or ``raise``, or convert to an explicit ``exhausted`` flag.
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
                            iteration_count=iteration_completed,
                        )
                    except Exception as exc:
                        self._log(
                            f"Failed to write exhausted guardrail call file: {exc}"
                        )
                        if rollback_exc is None:
                            raise GuardrailRepairFailed(
                                f"Guardrail repair exhausted after {self._max_repair_iterations} "
                                f"iterations and call file could not be written: {exc}",
                                failure_reason=FailureReason.GUARDRAIL_REPAIR_EXHAUSTED_CALL_FAILED,
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
                        # B11 fix: render call_file defensively so a
                        # logic error that left it None never produces
                        # the literal string "None" in operator output.
                        call_file_str = (
                            str(call_file) if call_file is not None
                            else "<unwritten>"
                        )
                        raise GuardrailRollbackError(
                            f"Guardrail repair exhausted after {self._max_repair_iterations} "
                            f"iterations but rollback failed. The human call file "
                            f"was written at {call_file_str} for diagnostic evidence.",
                            call_file=call_file,
                        ) from rollback_exc

                    raise GuardrailRepairExhausted(
                        f"Guardrail repair exhausted after {self._max_repair_iterations} "
                        f"iteration(s): LLM could not reduce violations",
                        call_file=call_file,
                        iteration_count=iteration_completed,
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
        except GuardrailCallFileError:
            raise  # re-raise so caller surfaces the call-file write failure
        except GuardrailNoRollbackError:
            raise  # re-raise so caller surfaces the no-rollback signal
        except GuardrailRepairInconsistentState:
            raise  # re-raise so caller can pin INCONSISTENT_REPAIR_STATE
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
                    FailureReason.GUARDRAIL_CHECK_FAILED_AND_ROLLBACK_FAILED
                    if not rollback_ok else FailureReason.GUARDRAIL_CHECK_FAILED
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
                # G3: include traceback for both the rollback exception
                # and the outer guardrails exception so post-mortem
                # analysis can see both stacks. ``raise ... from exc``
                # only chains the outer guardrails exception; the
                # rollback exception's stack is otherwise lost.
                self._log(
                    f"Rollback also failed after guardrails check crash: {rbe}",
                    level=logging.ERROR,
                    exc_info=True,
                )
                raise GuardrailRollbackError(
                    f"Guardrails check itself crashed for '{branch}': {exc}. "
                    f"Rollback also failed: {rbe!r}. "
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

    def _preflight_dirty_tracked_files(
        self, report: MergeReport, branches: list[str]
    ) -> bool:
        """Ensure the main working tree is clean enough for merge to START.

        A running session opens/closes issues in the MAIN repository between
        commit steps, leaving se3/issues/ files and se3/issues/.next_id dirty.
        When a branch being merged touched the same file (typically .next_id),
        git refuses to even begin the merge ("Your local changes would be
        overwritten by merge") — and an UNCOMMITTED change is not a merge side,
        so the deterministic/LLM resolvers never get a chance to run.

        Resolution: if every dirty tracked path is self-managed — under a
        directory prefix (:data:`_SELF_MANAGED_DIRTY_PREFIXES`) or an exact
        file entry (:data:`_SELF_MANAGED_DIRTY_FILES`) — auto-commit it as
        "chore: sync issue state" so the .next_id divergence becomes an
        ordinary three-way conflict that NextIdResolver (max-of-two-counters)
        resolves. Any dirty tracked path OUTSIDE the whitelist is a genuine
        operator-state problem we must not silently commit, so we fail loud
        with :data:`FailureReason.DIRTY_WORKING_TREE` and the file list.

        Must run INSIDE the merge lock, AFTER _check_repo_state, and BEFORE the
        pre_merge_sha capture — so the sync commit is part of the rollback
        baseline (_rollback_to can never discard issue state) and the
        pre-merge version read happens on the correct HEAD. The caller owns the
        _write_log / report.log_file bookkeeping on the False path.

        Returns:
            True if the merge may proceed (clean, or self-managed files were
            committed); False if a structured failure was written to *report*.
        """
        try:
            status_result = _run_git(
                self.project_root,
                "status", "--porcelain=v1", "-uno", "-z",
                check=False, timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            self._log(
                f"git status (dirty pre-flight) timed out: {exc}",
                level=logging.ERROR,
            )
            report.success = False
            report.failure_reason = FailureReason.UNEXPECTED.legacy_string
            report.failure_detail = f"dirty pre-flight git status timed out: {exc}"
            report.unattempted_branches = list(branches)
            return False
        if status_result.returncode != 0:
            self._log(
                "git status (dirty pre-flight) failed: "
                f"{redact_text(status_result.stderr.strip())}",
                level=logging.ERROR,
            )
            report.success = False
            report.failure_reason = FailureReason.UNEXPECTED.legacy_string
            report.failure_detail = (
                "dirty pre-flight git status returned "
                f"{status_result.returncode}: {status_result.stderr.strip()}"
            )
            report.unattempted_branches = list(branches)
            return False

        # Parse the NUL-separated porcelain-v1 records. Each record is
        # "XY <path>"; a rename/copy (R/C in the status field) carries its
        # source path in the FOLLOWING NUL-separated token, so we consume two
        # tokens and require BOTH ends to be self-managed. -z avoids the "->"
        # arrow and quoting ambiguity for paths with spaces.
        tokens = status_result.stdout.split("\0")
        all_paths: list[str] = []
        outside_paths: list[str] = []
        conflict_paths: list[str] = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if not tok:
                i += 1
                continue
            xy = tok[:2]
            # Unmerged (conflict) entries — a 'U' in either column, or
            # both-added/both-deleted (AA/DD) — mean a merge is ALREADY in
            # progress in the main working tree. We must never start a second
            # merge on top of one: the conflict markers can't be swept into a
            # sync commit (that would commit broken content) and starting a
            # new `git merge` on an in-flight merge is exactly the state this
            # pre-flight exists to refuse. So we treat every conflicted path as
            # a hard blocker, listed in the dirty_working_tree failure detail,
            # regardless of whether it falls under a self-managed prefix.
            if "U" in xy or xy == "AA" or xy == "DD":
                conflict_paths.append(tok[3:])
                all_paths.append(tok[3:])
                i += 1
                continue
            entry_paths = [tok[3:]]
            if "R" in xy or "C" in xy:
                # Rename/copy: the second (source) path is the next token.
                i += 1
                if i < len(tokens) and tokens[i]:
                    entry_paths.append(tokens[i])
            for p in entry_paths:
                all_paths.append(p)
                if not _is_self_managed_dirty_path(p):
                    outside_paths.append(p)
            i += 1

        if not all_paths:
            # Clean (tracked) working tree — proceed. Untracked-only files do
            # not block merge start, so we deliberately leave them alone.
            return True

        if outside_paths or conflict_paths:
            conflict_sorted = sorted(set(conflict_paths))
            outside_sorted = sorted(set(outside_paths) - set(conflict_paths))
            inside_sorted = sorted(
                set(all_paths) - set(outside_paths) - set(conflict_paths)
            )
            block_reasons = []
            if conflict_sorted:
                block_reasons.append("an unresolved merge is in progress")
            if outside_sorted:
                block_reasons.append(
                    "dirty tracked files exist outside SE3 self-managed paths"
                )
            self._log(
                "Refusing to start merge: "
                + " and ".join(block_reasons)
                + f": {', '.join(conflict_sorted + outside_sorted)}",
                level=logging.ERROR,
            )
            detail_lines = [
                "Cannot start merge: the main working tree is not in a clean "
                "state; resolve/commit or restore the paths below first.",
            ]
            if conflict_sorted:
                detail_lines.append(
                    "Unresolved merge conflicts (a merge is already in "
                    "progress):"
                )
                detail_lines += [f"  - {p}" for p in conflict_sorted]
            if outside_sorted:
                detail_lines.append("Dirty tracked files outside self-managed paths:")
                detail_lines += [f"  - {p}" for p in outside_sorted]
            if inside_sorted:
                detail_lines.append(
                    "Within self-managed paths "
                    f"({', '.join(_SELF_MANAGED_DIRTY_PREFIXES + _SELF_MANAGED_DIRTY_FILES)}):"
                )
                detail_lines += [f"  - {p}" for p in inside_sorted]
            report.success = False
            report.failure_reason = FailureReason.DIRTY_WORKING_TREE.legacy_string
            report.failure_detail = "\n".join(detail_lines)
            report.unattempted_branches = list(branches)
            return False

        # All dirty tracked files are self-managed: auto-commit them so the
        # divergence enters the three-way merge as a real "ours" side. Use
        # ``git add -A -- se3/issues`` so newly-opened (untracked) issue yaml
        # files — which always accompany a .next_id bump — are swept in too,
        # making the sync commit semantically complete.
        self._log(
            "Auto-committing dirty self-managed issue state before merge: "
            f"{', '.join(sorted(set(all_paths)))}"
        )
        # Derive the add pathspecs from the dirty tracked paths that actually
        # need committing, NOT from a whitelist entry merely existing on disk.
        # Two ways the on-disk-existence heuristic went wrong:
        #   * a whitelist FILE that exists but is gitignored-untracked (e.g.
        #     se3/code-index.md on a pre-migrate .gitignore that never
        #     whitelisted it) makes `git add` fatal with "paths are ignored",
        #     aborting a sync whose every dirty file was under se3/issues/;
        #   * an ABSENT file entry makes `git add` fatal with "did not match".
        # Including a whitelist entry only when a dirty tracked path matches it
        # sidesteps both. Directory entries add the dir pathspec so newly-opened
        # (untracked) issue yaml under it is swept in with the .next_id bump;
        # file entries add the exact tracked path (which git add accepts even if
        # a .gitignore pattern would otherwise cover it).
        add_targets: list[str] = []
        for pref in _SELF_MANAGED_DIRTY_PREFIXES:
            if any(p.startswith(pref) for p in all_paths):
                add_targets.append(pref.rstrip("/"))
        for exact in _SELF_MANAGED_DIRTY_FILES:
            if exact in all_paths:
                add_targets.append(exact)
        try:
            add_result = _run_git(
                self.project_root,
                "add", "-A", "--", *add_targets,
                check=False, timeout=30,
            )
            if add_result.returncode != 0:
                report.success = False
                report.failure_reason = FailureReason.DIRTY_WORKING_TREE.legacy_string
                report.failure_detail = (
                    f"auto-commit failed: git add returned "
                    f"{add_result.returncode}: {add_result.stderr.strip()}"
                )
                report.unattempted_branches = list(branches)
                return False
            commit_result = _run_git(
                self.project_root,
                "commit", "-m", "chore: sync issue state",
                check=False, timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            report.success = False
            report.failure_reason = FailureReason.DIRTY_WORKING_TREE.legacy_string
            report.failure_detail = f"auto-commit failed: git timed out: {exc}"
            report.unattempted_branches = list(branches)
            return False
        if commit_result.returncode != 0:
            # "nothing to commit" is NOT a failure: after `git add` the index
            # equals HEAD, so the whitelist is effectively clean (e.g. a
            # self-managed file was edited then restored to its HEAD content
            # without unstaging). The goal is to auto-commit and START the
            # merge, not to block an already-mergeable tree — so treat it as
            # success and proceed. git prints "nothing to commit" on stdout.
            combined = f"{commit_result.stdout} {commit_result.stderr}".lower()
            if "nothing to commit" in combined:
                self._log(
                    "Auto-commit found nothing to commit (index equals HEAD) "
                    "— working tree effectively clean, proceeding with merge"
                )
                return True
            report.success = False
            report.failure_reason = FailureReason.DIRTY_WORKING_TREE.legacy_string
            report.failure_detail = (
                f"auto-commit failed: git commit returned "
                f"{commit_result.returncode}: {commit_result.stderr.strip()}"
            )
            report.unattempted_branches = list(branches)
            return False
        self._log("Committed 'chore: sync issue state' — proceeding with merge")
        return True

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
        # A non-zero rc when there is simply no merge in progress is NOT a
        # failure: the target state (no merge residue in the working tree)
        # already holds. git emits "fatal: There is no merge to abort
        # (MERGE_HEAD missing)." with rc=128 in that case. Treating it as
        # success is what keeps a merge that never STARTED (e.g. blocked
        # pre-flight) from having its real failure_reason overwritten with a
        # misleading merge_abort_failed. Match both stable substrings, case-
        # insensitively, against combined stdout+stderr.
        combined = f"{abort_result.stdout} {abort_result.stderr}".lower()
        if "no merge to abort" in combined or "merge_head missing" in combined:
            self._log(
                "git merge --abort: no merge in progress — treating abort as success"
            )
            return True
        self._log(f"git merge --abort failed: {redact_text(abort_result.stderr.strip())}")
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


def integrate(
    project_root: Path,
    branches: list[str],
    *,
    strategy: str = "fast",
    delete_merged: bool = True,
    strict_runtime_sync: bool = False,
    acquire_lock: bool = True,
    suppress_human_call: bool = True,
) -> MergeResult:
    """Library entry point: integrate *branches* into the current branch.

    Wraps the sequential branch-merge machinery — git merge + LLM conflict
    resolution + runtime sync + issue renumber + post-condition checks — and
    returns a structured :class:`MergeResult`. This is the "integrate" half of
    the merge-library split: it owns the *invariants* (merge lock, runtime sync,
    renumber, post-conditions) while leaving *flow-control* to the caller.

    Unlike the legacy top-level orchestrator entry it does NOT write human-call
    files or print terminal instructions. An escalation is surfaced on the
    returned report instead (``pending_human=True``; the recorded payloads live
    on the orchestrator's ``_RecordingNullHumanCallWriter``), so the caller — a
    flow step (PAUSED/confirm/resume) or the ``se3 merge`` CLI (exit codes) —
    decides what happens next.

    Failure is expressed in the returned :class:`MergeResult`
    (``success=False`` + ``failure_reason``/``failure_detail``), or, for
    programmer/state errors the orchestrator already raises (detached HEAD,
    shallow repo, lock-busy), as the same typed exceptions — never as a
    written call file.

    Args:
        project_root: The main-checkout project root (the merge target).
        branches: Branches to merge, in order.
        strategy: Conflict-resolution strategy tier (``"fast"`` default).
        delete_merged: Delete each branch after a successful merge.
        strict_runtime_sync: Fail (rather than bypass) on runtime-sync
            collisions.
        acquire_lock: Acquire the process-wide merge lock inside ``execute``.
            Pass ``False`` when the caller already holds it.
        suppress_human_call: Run in library mode (default) — record escalations
            on the result instead of writing ``se3/calls/`` files or printing
            terminal instructions. Passed through so the CLI adapter can keep
            the worktree merge-back's legacy call-file behaviour by supplying
            ``False``.

    Returns:
        The :class:`MergeResult` from the underlying orchestration run.
    """
    orchestrator = MergeOrchestrator(
        project_root=project_root,
        strategy=strategy,
        delete_merged=delete_merged,
        strict_runtime_sync=strict_runtime_sync,
        acquire_lock=acquire_lock,
        suppress_human_call=suppress_human_call,
    )
    report = orchestrator.execute(branches)
    # Expose the recorded escalations on the result so both entry points (the
    # ``se3 merge`` CLI, a flow step) can consume what would have been written
    # to ``se3/calls/`` without any file having been created — the caller owns
    # flow-control. Best-effort: a MergeReport is a plain dataclass so the
    # dynamic attribute is always settable; only skip on the theoretical case
    # of a slotted/immutable report substituted by a test double.
    try:
        report.recorded_escalations = orchestrator.recorded_escalations
    except (AttributeError, TypeError):
        pass
    return report
