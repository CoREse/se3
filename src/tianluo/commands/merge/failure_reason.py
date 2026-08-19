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

The legacy string surface (used by ``MergeReport.failure_reason`` and a
small number of test fixtures) remains a public contract for the duration
of the deprecation window. Callers that have already migrated to the typed
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

    # 5xx was the post-merge spec-guardrails family. The guardrails chain is
    # gone, so no new report carries those reasons; ``from_legacy_string``
    # still resolves any that survive in an archived report by falling
    # through to ``UNEXPECTED`` with the raw spelling preserved as detail.

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
    POSTCOND_BRANCH_UNRESOLVABLE = 903

    # --- 91x: silent merge loss / timeout variants ----------------------
    SILENT_MERGE_LOSS = 910
    POSTCOND_CHECK_TIMEOUT = 911

    # --- 92x: version anomaly (on-disk version > computed target) --------
    VERSION_HIGHER_THAN_TARGET = 920
    # On-disk version equals computed target before aggregator can write —
    # the bump came from somewhere other than this aggregator run (prior
    # manual bump or a branch tip whose pyproject.toml already had the
    # target). Fail-loud so a silent no-op cannot be confused with a
    # successful aggregator-driven bump. See version_aggregator.py C1
    # docs and orchestrator.py equal-version handling.
    VERSION_ALREADY_AT_TARGET = 921

    # --- 93x: unsupported repository state (K5/K6 fail-fast) --------------
    REPO_EMPTY = 930
    REPO_DETACHED_HEAD = 931
    REPO_SHALLOW = 932
    REPO_UNSUPPORTED_STATE = 933
    # Merge could not even start because the main working tree has dirty
    # tracked files outside the SE3 self-managed data paths (tianluo/issues/).
    # Same fail-fast family as the REPO_* states above — a pre-merge
    # repository condition the operator must clear before merge can run.
    DIRTY_WORKING_TREE = 934

    # --- 98x: input validation ------------------------------------------
    NO_BRANCHES = 980

    # --- 99x: lock contention -------------------------------------------
    LOCK_BUSY = 990
    LOCK_STALE = 991

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
    "postcond_branch_unresolvable": FailureReason.POSTCOND_BRANCH_UNRESOLVABLE,
    # 91x
    "silent_merge_loss": FailureReason.SILENT_MERGE_LOSS,
    "silent_merge_loss_branch_unresolvable": FailureReason.POSTCOND_BRANCH_UNRESOLVABLE,
    "postcond_check_timeout": FailureReason.POSTCOND_CHECK_TIMEOUT,
    # 92x
    "version_higher_than_target": FailureReason.VERSION_HIGHER_THAN_TARGET,
    "version_already_at_target": FailureReason.VERSION_ALREADY_AT_TARGET,
    # 93x
    "repo_empty": FailureReason.REPO_EMPTY,
    "repo_detached_head": FailureReason.REPO_DETACHED_HEAD,
    "repo_shallow": FailureReason.REPO_SHALLOW,
    "repo_unsupported_state": FailureReason.REPO_UNSUPPORTED_STATE,
    "dirty_working_tree": FailureReason.DIRTY_WORKING_TREE,
    # Legacy spellings derived from exception class names.  Older code
    # paths derived these strings via ``type(exc).__name__.replace(
    # "Error", "").lower()``; the explicit alias keeps the on-disk
    # surface stable across exception-class renames.
    "emptyrepo": FailureReason.REPO_EMPTY,
    "detachedhead": FailureReason.REPO_DETACHED_HEAD,
    "shallowrepo": FailureReason.REPO_SHALLOW,
    "unsupportedrepostate": FailureReason.REPO_UNSUPPORTED_STATE,
    # 98x
    "no_branches": FailureReason.NO_BRANCHES,
    # 99x
    "lock_busy": FailureReason.LOCK_BUSY,
    "lock_stale": FailureReason.LOCK_STALE,
    # catch-all
    "unexpected": FailureReason.UNEXPECTED,
}

# Compound prefixes auto-derived from _LEGACY_STRING_MAP so that every
# key is a potential compound prefix.  A contributor adding a new legacy
# string does not need to remember to dual-register it in a separate
# tuple — the key is automatically eligible for compound matching.
# Order matters: the longest matching prefix wins so a contributor
# adding a longer prefix that shares a head with a shorter one (e.g.
# ``"merge_failed_detached"`` shadowing ``"merge_failed"``) cannot
# accidentally introduce shadowing.  We sort by descending prefix
# length at module-load time so the iteration in
# :func:`from_legacy_string` always tests longest-first regardless of
# how a contributor wrote the source list.
_COMPOUND_PREFIXES: tuple[tuple[str, FailureReason], ...] = tuple(
    sorted(
        ((k, v) for k, v in _LEGACY_STRING_MAP.items() if k),
        key=lambda entry: len(entry[0]),
        reverse=True,
    )
)


def _assert_compound_prefix_order_longest_first() -> None:
    """Defensive assertion: longer prefixes precede shorter prefixes
    that they share a head with.

    Importable from tests; raises :class:`AssertionError` on violation
    so a future contributor's hand-curated insertion that breaks the
    longest-first invariant is caught before it produces silent
    shadowing.
    """
    by_index = list(_COMPOUND_PREFIXES)
    for i, (a_prefix, _) in enumerate(by_index):
        for b_prefix, _ in by_index[i + 1:]:
            # In a longest-first sorted list, a_prefix (earlier) is always
            # longer than or equal to b_prefix (later).  The only shadowing
            # risk is when b_prefix is longer than a_prefix but appears later
            # — impossible with descending-length sort, but we keep the check
            # as a safety net against manual reordering or a future bug.
            if b_prefix.startswith(a_prefix) and len(b_prefix) > len(a_prefix):
                raise AssertionError(
                    f"_COMPOUND_PREFIXES ordering broken: "
                    f"{b_prefix!r} (len {len(b_prefix)}) is longer than "
                    f"{a_prefix!r} (len {len(a_prefix)}) but appears later — "
                    f"shorter prefix would shadow longer prefix at iteration "
                    f"time."
                )


# Run the assertion at module load so any developer who edits the
# tuple sees the error immediately rather than waiting for a test run.
_assert_compound_prefix_order_longest_first()


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
