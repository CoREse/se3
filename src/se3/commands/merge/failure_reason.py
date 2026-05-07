"""FailureReason enum — typed replacement for the merge orchestrator's
string ``failure_reason`` field.

The legacy orchestrator scattered ~60 distinct string literals across
``orchestrator.py`` to mark individual failure modes. This module
collects them into a single ``IntEnum`` so that:

  * the set of valid values is enumerable and discoverable,
  * downstream callers can switch on a typed value rather than fragile
    string comparisons,
  * compound diagnostic strings (e.g. ``"fast_abort: <stderr>"``) are
    decomposed into a base reason plus a separate detail string,
  * a one-release compatibility window is preserved via
    ``from_legacy_string``, which accepts both the bare reason and the
    compound prefix forms used in older code paths.

The legacy string surface (used by ``MergeReport.failure_reason``,
``GuardrailRepairFailed.failure_reason`` and a small number of test
fixtures) remains a public contract for the duration of the
deprecation window. Callers that have already migrated to the typed
enum SHOULD use :class:`FailureReason` directly; callers that have not
SHOULD use :func:`from_legacy_string` to translate legacy strings into
enum values.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Optional, Tuple


class FailureReason(IntEnum):
    """Enumerated reasons a merge orchestration may fail or pause.

    Values are assigned with explicit, gap-aware integers grouped by
    subsystem so that new values can be inserted without renumbering
    existing entries. The numeric values are NOT a stable wire format —
    persisted reports SHOULD use the enum *name* (a stable string
    identifier) rather than the integer.
    """

    # --- 0xx: clean exit, no failure -----------------------------------
    NONE = 0

    # --- 1xx: pending-human (paused, awaiting operator input) -----------
    PENDING_HUMAN = 100

    # --- 2xx: git-level merge failures ----------------------------------
    MERGE_CONFLICT = 200
    MERGE_FAILED = 201
    MERGE_TIMED_OUT = 202
    MERGE_ABORT_FAILED = 203

    # --- 3xx: conflict-resolution / LLM-resolution failures -------------
    CONFLICT_CONTEXT_FAILED = 300
    CONFLICT_CONTEXT_FAILED_CALL_FILE_WRITE_FAILED = 301
    LLM_RESOLUTION_FAILED = 302
    INCOMPLETE_RESOLUTION = 303
    INCOMPLETE_RESOLUTION_CALL_FAILED = 304
    RESOLUTION_REJECTED = 305
    RESOLUTION_VALIDATION_FAILED = 306
    RESOLUTION_WRITE_FAILED = 307
    RESOLUTION_COMMIT_FAILED = 308
    RESOLUTION_COMMIT_TIMEOUT = 309
    BINARY_FILE_CONFLICT = 310
    BINARY_FILE_CONFLICT_FAST_ABORT = 311

    # --- 4xx: fast-strategy aborts (the LLM auto-resolution path) -------
    FAST_ABORT = 400
    FAST_FAILURE = 401

    # --- 5xx: guardrail violations (post-merge spec checks) -------------
    GUARDRAIL_VIOLATION = 500
    GUARDRAIL_VIOLATION_NO_ROLLBACK = 501
    GUARDRAIL_VIOLATION_CALL_FAILED = 502
    GUARDRAIL_CHECK_FAILED = 503
    GUARDRAIL_CHECK_FAILED_AND_ROLLBACK_FAILED = 504
    GUARDRAIL_MISSING_PRE_SHA = 505
    GUARDRAIL_MISSING_POST_SHA = 506
    GUARDRAIL_MISSING_PRE_AND_POST_SHA = 507
    GUARDRAIL_REPAIR_FAILED = 510
    GUARDRAIL_REPAIR_STALLED = 511
    GUARDRAIL_REPAIR_STALLED_CALL_FAILED = 512
    GUARDRAIL_REPAIR_EXHAUSTED = 513
    GUARDRAIL_REPAIR_EXHAUSTED_CALL_FAILED = 514

    # --- 6xx: rollback failures -----------------------------------------
    ROLLBACK_FAILED = 600

    # --- 7xx: human-call write failures (orphan: rollback OK, call x) ---
    HUMAN_CALL_WRITE_FAILED = 700

    # --- 8xx: post-merge runtime data sync failures ---------------------
    RUNTIME_SYNC_COLLISION = 800
    RUNTIME_SYNC_OS_ERROR = 801
    RUNTIME_SYNC_TIMEOUT = 802

    # --- 9xx: post-condition violations (G1 introduced) -----------------
    POSTCOND_BRANCH_NOT_MERGED = 900
    POSTCOND_HEAD_NOT_MERGE_COMMIT = 901
    POSTCOND_VERSION_NOT_BUMPED = 902

    # --- 99x: lock contention -------------------------------------------
    LOCK_BUSY = 990

    # --- catch-all -------------------------------------------------------
    UNEXPECTED = 9999

    @property
    def legacy_string(self) -> str:
        """Return the legacy-string spelling for this reason.

        Each enum member has exactly one canonical legacy spelling,
        which is the lower-cased name. Compound diagnostic strings
        (e.g. ``"fast_abort: <stderr>"``) are NOT preserved here —
        callers that need to attach diagnostic detail SHOULD store it
        in a separate field.
        """
        return self.name.lower()


# A frozen mapping of the legacy strings actually emitted by the
# orchestrator and friends. The values may include strings whose enum
# spelling differs from a naive lowercased-name (none currently, but
# the indirection exists so future renames don't break compatibility).
_LEGACY_STRING_MAP: dict[str, FailureReason] = {
    # 0xx
    "": FailureReason.NONE,
    "none": FailureReason.NONE,
    # 1xx
    "pending_human": FailureReason.PENDING_HUMAN,
    # 2xx
    "merge_conflict": FailureReason.MERGE_CONFLICT,
    "merge_failed": FailureReason.MERGE_FAILED,
    "merge_timed_out": FailureReason.MERGE_TIMED_OUT,
    "merge_abort_failed": FailureReason.MERGE_ABORT_FAILED,
    # 3xx
    "conflict_context_failed": FailureReason.CONFLICT_CONTEXT_FAILED,
    "conflict_context_failed_call_file_write_failed": (
        FailureReason.CONFLICT_CONTEXT_FAILED_CALL_FILE_WRITE_FAILED
    ),
    "llm_resolution_failed": FailureReason.LLM_RESOLUTION_FAILED,
    "incomplete_resolution": FailureReason.INCOMPLETE_RESOLUTION,
    "incomplete_resolution_call_failed": (
        FailureReason.INCOMPLETE_RESOLUTION_CALL_FAILED
    ),
    "resolution_rejected": FailureReason.RESOLUTION_REJECTED,
    "resolution_validation_failed": FailureReason.RESOLUTION_VALIDATION_FAILED,
    "resolution_write_failed": FailureReason.RESOLUTION_WRITE_FAILED,
    "resolution_commit_failed": FailureReason.RESOLUTION_COMMIT_FAILED,
    "resolution_commit_timeout": FailureReason.RESOLUTION_COMMIT_TIMEOUT,
    "binary_file_conflict": FailureReason.BINARY_FILE_CONFLICT,
    "binary_file_conflict_fast_abort": FailureReason.BINARY_FILE_CONFLICT_FAST_ABORT,
    # 4xx
    "fast_abort": FailureReason.FAST_ABORT,
    "fast_failure": FailureReason.FAST_FAILURE,
    # 5xx
    "guardrail_violation": FailureReason.GUARDRAIL_VIOLATION,
    "guardrail_violation_no_rollback": FailureReason.GUARDRAIL_VIOLATION_NO_ROLLBACK,
    "guardrail_violation_call_failed": FailureReason.GUARDRAIL_VIOLATION_CALL_FAILED,
    "guardrail_check_failed": FailureReason.GUARDRAIL_CHECK_FAILED,
    "guardrail_check_failed_and_rollback_failed": (
        FailureReason.GUARDRAIL_CHECK_FAILED_AND_ROLLBACK_FAILED
    ),
    "guardrail_missing_pre_sha": FailureReason.GUARDRAIL_MISSING_PRE_SHA,
    "guardrail_missing_post_sha": FailureReason.GUARDRAIL_MISSING_POST_SHA,
    "guardrail_missing_pre_and_post_sha": (
        FailureReason.GUARDRAIL_MISSING_PRE_AND_POST_SHA
    ),
    "guardrail_repair_failed": FailureReason.GUARDRAIL_REPAIR_FAILED,
    "guardrail_repair_stalled": FailureReason.GUARDRAIL_REPAIR_STALLED,
    "guardrail_repair_stalled_call_failed": (
        FailureReason.GUARDRAIL_REPAIR_STALLED_CALL_FAILED
    ),
    "guardrail_repair_exhausted": FailureReason.GUARDRAIL_REPAIR_EXHAUSTED,
    "guardrail_repair_exhausted_call_failed": (
        FailureReason.GUARDRAIL_REPAIR_EXHAUSTED_CALL_FAILED
    ),
    # 6xx
    "rollback_failed": FailureReason.ROLLBACK_FAILED,
    # 7xx
    "human_call_write_failed": FailureReason.HUMAN_CALL_WRITE_FAILED,
    # 8xx
    "runtime_sync_collision": FailureReason.RUNTIME_SYNC_COLLISION,
    "runtime_sync_os_error": FailureReason.RUNTIME_SYNC_OS_ERROR,
    "runtime_sync_timeout": FailureReason.RUNTIME_SYNC_TIMEOUT,
    # 9xx
    "postcond_branch_not_merged": FailureReason.POSTCOND_BRANCH_NOT_MERGED,
    "postcond_head_not_merge_commit": FailureReason.POSTCOND_HEAD_NOT_MERGE_COMMIT,
    "postcond_version_not_bumped": FailureReason.POSTCOND_VERSION_NOT_BUMPED,
    # 99x
    "lock_busy": FailureReason.LOCK_BUSY,
    # catch-all
    "unexpected": FailureReason.UNEXPECTED,
}

# Compound prefixes that carry diagnostic detail after a colon.
# Order matters: the longest matching prefix wins, so list specific
# variants before their shorter siblings.
_COMPOUND_PREFIXES: tuple[tuple[str, FailureReason], ...] = (
    ("fast_failure", FailureReason.FAST_FAILURE),
    ("fast_abort", FailureReason.FAST_ABORT),
    ("merge_failed", FailureReason.MERGE_FAILED),
)


def from_legacy_string(
    raw: Optional[str],
) -> Tuple[Optional[FailureReason], Optional[str]]:
    """Translate a legacy ``failure_reason`` string into a typed reason
    plus an optional diagnostic detail.

    Args:
        raw: The legacy string, possibly ``None`` (no failure), an empty
            string (no failure), a bare reason (e.g. ``"merge_conflict"``)
            or a compound prefix carrying detail (e.g.
            ``"fast_abort: subprocess timed out"``).

    Returns:
        A tuple ``(reason, detail)``:
          * ``reason`` is the typed :class:`FailureReason`, or ``None``
            if the input was ``None`` (use this to distinguish "no
            failure recorded" from ``FailureReason.NONE`` "explicitly
            no failure").
          * ``detail`` is the diagnostic string trailing the prefix
            for compound forms, or ``None`` for bare reasons.

    Unknown strings map to :data:`FailureReason.UNEXPECTED` with the
    raw input preserved verbatim in ``detail`` so the original spelling
    is not lost on the round trip.
    """
    if raw is None:
        return None, None
    text = raw.strip()
    if not text:
        return FailureReason.NONE, None

    # Exact match wins outright (no diagnostic detail).
    direct = _LEGACY_STRING_MAP.get(text)
    if direct is not None:
        return direct, None

    # Compound prefix match: prefix is followed by ':' or whitespace.
    for prefix, reason in _COMPOUND_PREFIXES:
        if text == prefix:
            return reason, None
        if text.startswith(prefix):
            tail = text[len(prefix):]
            # Only treat as compound if the tail starts with a
            # separator; otherwise the prefix was actually part of a
            # different identifier.
            if tail and tail[0] in (":", " "):
                detail = tail.lstrip(": ").strip()
                return reason, detail or None
            # Fall through: prefix matched but no separator follows —
            # treat as unknown so we don't accidentally swallow
            # something like "fast_abort_extra".

    # Final fall-back: preserve the raw string as detail so debugging
    # information is not lost.
    return FailureReason.UNEXPECTED, text


def to_legacy_string(reason: Optional[FailureReason]) -> Optional[str]:
    """Render a :class:`FailureReason` back into its legacy string form.

    Returns ``None`` for ``None``, empty for ``FailureReason.NONE`` (so
    that round-tripping via ``from_legacy_string`` preserves the
    "explicitly no failure" semantics for the empty-string variant).
    """
    if reason is None:
        return None
    if reason is FailureReason.NONE:
        return ""
    return reason.legacy_string
