"""HumanCallWriter — Create MCP human call files for merge conflicts.

Writes structured call files to ``tianluo/calls/`` when LLM conflict resolution
triggers a HUMAN_CALL decision. The call file contains conflict context,
LLM resolution proposals, and decision options for human review.
"""

from __future__ import annotations
from tianluo.runtime_paths import runtime_dir

import hashlib
import itertools
import json
import logging
import os
import subprocess
import tempfile
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ...commands.merge.secret_redact import redact_text
from .conflict_context import ConflictContext
from .conflict_resolver import LLMResolution
from .runtime_sync import _safe_branch_label
from .strategy import StrategyDecision

logger = logging.getLogger(__name__)

# Module-level atomic sequence counter for unique filenames within a process.
# Together with pid and microsecond timestamp, guarantees no collision even
# under millisecond-level concurrency (fixes defect F1).
#
# CAVEAT: ``importlib.reload(human_call)`` resets this counter to 0.  Within
# a single process this is normally fine because every previously-issued
# filename also embedded a microsecond-precision UTC timestamp and an 8-char
# SHA — so a same-millisecond pid+seq+sha8 collision after a reload remains
# astronomically unlikely.  Test frameworks that exercise reload semantics
# should use :func:`importlib.reload` only when the file system is empty or
# when test fixtures rotate ``calls_dir``.
#
# FORK CAVEAT: ``itertools.count`` is fork-unsafe — a child process inherits
# the same counter state as the parent and would emit duplicate seq numbers.
# luo merge does not currently fork, but any future change that introduces
# ``multiprocessing`` or ``os.fork`` after this counter has been advanced
# would otherwise re-emit the same pid+seq pair from the child.  The pid+sha8
# components of the filename make the practical collision probability low
# but not zero.
#
# Defense: register an ``os.register_at_fork`` handler that resets the
# counter in the child after fork.  ``os.register_at_fork`` is POSIX-only
# (not present on Windows); the registration is wrapped in ``hasattr`` so
# the import does not blow up on platforms without it.
_call_seq = itertools.count()
# Lock guarding ``_call_seq`` so multi-threaded callers (e.g. tests
# that drive concurrent write_call() in tight loops) cannot race on
# ``next(_call_seq)``.  CPython's GIL makes ``itertools.count.__next__``
# effectively atomic in production today, but tying the seq advance
# to an explicit lock removes the dependency on that implementation
# detail and makes the contract explicit for non-CPython runtimes
# (PyPy, future no-GIL CPython).
_call_seq_lock = threading.Lock()


def _reset_call_seq_after_fork_in_child() -> None:
    """Reset the per-process call-seq counter in a forked child.

    Called via :func:`os.register_at_fork` (POSIX only).  Without this
    reset the child would inherit the parent's counter state and could
    emit identical (pid_in_child, seq) pairs to (pid_in_parent_at_fork,
    seq_at_fork) — a low-probability but nonzero collision because the
    pid space is not perfectly disjoint after long-running pid recycling.

    Resets the lock as well so the child does not inherit a held lock
    from a thread that no longer exists in the child (Python's
    multiprocessing fork-then-thread story).
    """
    global _call_seq, _call_seq_lock
    _call_seq = itertools.count()
    _call_seq_lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_call_seq_after_fork_in_child)


def _generate_call_filename(prefix: str, branch: str) -> str:
    """Generate a collision-resistant call filename.

    Format::

        <prefix>_<utc_iso>_<pid>_<seq>_<sha8>_<safe_branch>.json

    Uses UTC timestamp with microsecond precision, process ID, an atomic
    sequence counter, and an 8-char SHA256 hash for additional entropy.
    Even 100 concurrent calls within the same millisecond are guaranteed
    unique because ``seq`` is an atomic process-level counter.

    The branch name is sanitized via ``_safe_branch_label`` (replaces
    ``/``, ``\\``, ASCII control chars, and any character outside
    ``[A-Za-z0-9._-]``) so that maliciously-named branches cannot produce
    paths that break listing tools.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
    pid = os.getpid()
    # Acquire the lock so concurrent threads cannot race on the
    # counter advance.  CPython's GIL would normally make ``next``
    # atomic, but the explicit lock removes that dependency and
    # guarantees uniqueness on non-CPython runtimes.
    with _call_seq_lock:
        seq = next(_call_seq)
    hash_input = f"{branch}:{ts}:{pid}:{seq}"
    sha8 = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()[:8]
    safe_branch = _safe_branch_label(branch)
    return f"{prefix}_{ts}_{pid}_{seq}_{sha8}_{safe_branch}.json"


def _atomic_write_json(call_file: Path, call_data: dict) -> None:
    """Atomically write JSON call data to ``call_file``.

    Uses a temporary file in the same directory + ``os.replace`` so that
    readers never observe a partially-written file.  Includes fsync on
    the file descriptor and parent directory to survive power loss
    (fixes defect F2).

    Symlink protection: if a symlink exists at the destination path,
    refuse to write rather than letting ``os.replace`` follow / replace
    it.  ``tianluo/calls/`` is normally controlled, but since we already
    take pains with mkstemp prefix and fsync, the destination check
    closes the symmetry gap so a malicious or accidentally-placed
    symlink cannot redirect the write to an unrelated path.

    On a successful first write the destination does not exist yet, so
    the lstat check is a no-op for new call files.  On rare
    overwrite-of-existing paths, an existing regular file passes through
    unchanged via ``os.replace``; only a symlink at the target is
    rejected.

    Original mode preservation: when overwriting an existing file we
    preserve its permission bits (``os.chmod`` after rename) so call
    files that other tools (CI runners, MCP clients running as different
    users) need to read remain readable.  When creating a new file the
    mkstemp default of 0o600 is widened to 0o644 — a call-file
    convention consistent with the rest of ``tianluo/calls/`` — so the
    common case (no pre-existing file) is not silently restricted.
    """
    call_file.parent.mkdir(parents=True, exist_ok=True)

    # Symlink protection on the destination.  ``os.path.islink`` follows
    # only the final component; if the path is a symlink we refuse,
    # otherwise (regular file or absent) we allow the write to proceed.
    if call_file.is_symlink():
        raise OSError(
            f"Refusing to write call file {call_file}: destination is a "
            f"symlink (would follow to an unintended path)"
        )

    # Capture the original file mode (if any) so we can preserve it on
    # rename.  When the file is absent (the common case for new calls),
    # default to 0o644 so the file is readable to other users — matching
    # version_aggregator._atomic_write_text's preserve-or-readable
    # convention.
    original_mode: Optional[int] = None
    try:
        original_mode = call_file.stat().st_mode & 0o777
    except OSError:
        original_mode = None

    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=call_file.parent,
        prefix=f".tmp_{call_file.name}_",
    )
    dir_fd: Optional[int] = None
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(call_data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(tmp_fd)
        # Set the temp file's mode before the rename so the target file
        # ends up with the intended bits without a brief 0o600 window.
        try:
            if original_mode is not None:
                os.chmod(tmp_path, original_mode)
            else:
                os.chmod(tmp_path, 0o644)
        except OSError:
            # Some filesystems disallow chmod on a newly-created file
            # (rare).  Fall through — the replace still succeeds and the
            # file inherits the mkstemp default.
            pass
        # Re-check destination for symlink right before replace to
        # narrow the TOCTOU window.  An attacker cannot guarantee the
        # symlink survives both lstat checks plus the replace, so this
        # is a best-effort defence consistent with the merge-lock and
        # runtime-sync modules.
        if call_file.is_symlink():
            raise OSError(
                f"Refusing to write call file {call_file}: destination "
                f"became a symlink between checks (TOCTOU race)"
            )
        os.replace(tmp_path, call_file)
        # fsync directory to ensure the rename is durable.
        # O_DIRECTORY is defined by POSIX.1-2008 and available on Linux,
        # modern *BSD, and macOS 10.10+ (HFS+/APFS).  On platforms where
        # it is missing, the OSError is silently ignored — the file data
        # itself has already been fsync'd above.
        try:
            dir_fd = os.open(str(call_file.parent), os.O_RDONLY | os.O_DIRECTORY)
            os.fsync(dir_fd)
        except OSError:
            pass  # directory fsync is best-effort on some filesystems
    except Exception:
        # Capture the original failure (chmod / replace / fsync) before
        # the unlink-attempt overwrites operator triage signal — without
        # this, the operator only sees the unlink errno and not the
        # underlying call-file write failure.
        logger.exception(
            "Failed to write call file %s; cleaning up tmp file %s",
            call_file, tmp_path,
        )
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    finally:
        if dir_fd is not None:
            try:
                os.close(dir_fd)
            except OSError:
                pass


def _read_original_for_orphan(
    project_root: Path, rel_path: str, base_ref: str,
) -> Optional[str]:
    """Read the original content of a file for orphan guardrails check.

    Tries ``base_ref`` first (pre-merge HEAD), then falls back to plain
    ``HEAD``.  Returns ``None`` when the file does not exist in either ref.
    """
    refs_to_try = []
    if base_ref:
        refs_to_try.append(base_ref)
    refs_to_try.append("HEAD")
    for ref in refs_to_try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "show", f"{ref}:{rel_path}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if result.returncode == 0:
            return result.stdout
    return None


def _is_spec_path(path: str) -> bool:
    """Return True when ``path`` matches ``tianluo/specs/**/spec.md``.

    Delegates to :func:`tianluo.engine.merge.guardrails._is_spec_path` so
    every module shares the same canonical implementation (defect from
    self-check round 7 — divergent regex / pathlib detectors caused
    silent bypasses across the merge subsystem).
    """
    from .guardrails import _is_spec_path as _canonical_is_spec_path

    return _canonical_is_spec_path(path)


def _scan_orphan_content_for_evidence(
    rel_path: str, resolved_content: str,
) -> dict:
    """Scan a rejected orphan's resolved content for guardrail-style evidence.

    F3 extension: orphan files that cannot undergo a diff-based guardrail
    check (non-spec paths or spec paths missing a base ref) still need
    structured evidence about WHAT the LLM smuggled in, so the human
    operator reviewing the call file can decide whether the content
    represents a bug or a legitimate creative resolution.

    Returns a dict with at minimum ``content_size``, ``content_preview``,
    plus optional flags surfacing suspicious patterns:
      - ``has_conflict_markers``: ``<<<<<<<`` / ``=======`` / ``>>>>>>>``
        leftover markers (the LLM should have resolved these).
      - ``has_spec_keywords``: spec-style normative keywords (SHALL,
        MUST, SHOULD, REQUIRED, REQUIREMENT, WHEN, THEN, GIVEN) found
        in a non-spec orphan path. Smuggling spec content into a
        non-spec file is a classic guardrail-evasion pattern.
      - ``oversize``: True when content exceeds 256 KB (1 MiB triggers
        ``critical_oversize``). Surfaces unbounded LLM responses.
      - ``looks_like_spec_path``: True when the path contains "spec"
        but does not match the strict ``tianluo/specs/**/spec.md`` pattern,
        indicating possible path-name evasion.
    """
    evidence: dict = {
        "content_size": len(resolved_content) if resolved_content else 0,
        "content_preview": (resolved_content[:500] if resolved_content else ""),
    }

    if not resolved_content:
        return evidence

    # Conflict markers — never legitimate in a "resolved" content payload.
    if (
        "<<<<<<<" in resolved_content
        or "=======" in resolved_content
        or ">>>>>>>" in resolved_content
    ):
        evidence["has_conflict_markers"] = True

    # Detect spec-style normative keywords. The list is intentionally
    # narrower than the full _WEAKEN_PATTERNS regex set to avoid
    # false positives — we only flag clearly-prescriptive keywords.
    import re as _re_mod
    _SPEC_KEYWORD_PATTERN = _re_mod.compile(
        r"\b(SHALL|MUST(?:\s+NOT)?|SHOULD(?:\s+NOT)?|REQUIRED|"
        r"REQUIREMENT|WHEN|THEN|GIVEN)\b"
    )
    matches = _SPEC_KEYWORD_PATTERN.findall(resolved_content)
    is_spec = _is_spec_path(rel_path)
    if matches and not is_spec:
        # Spec-style content in a non-spec orphan is suspicious.
        evidence["has_spec_keywords"] = True
        evidence["spec_keyword_count"] = len(matches)

    # Oversize content protection.
    size = len(resolved_content)
    if size > 256 * 1024:
        evidence["oversize"] = True
    if size > 1024 * 1024:
        evidence["critical_oversize"] = True

    # Path-name evasion: looks like a spec path but isn't strictly one.
    if not is_spec and "spec" in rel_path.lower():
        evidence["looks_like_spec_path"] = True

    return evidence


class HumanCallWriter:
    """Write MCP call files for human conflict resolution."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def write_call(
        self,
        context: ConflictContext,
        resolution: Optional[LLMResolution],
        decision: StrategyDecision,
        *,
        options: Optional[dict[str, str]] = None,
        instructions_override: Optional[str] = None,
        call_file_name: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> Path:
        """Write a human call file for the current merge conflict.

        Args:
            context: The three-way merge context.
            resolution: The LLM's proposed resolution. May be ``None`` when
                the strict strategy short-circuits LLM resolution.
            decision: The strategy decision that led to human escalation.
            options: Optional custom options dict to override the default
                ``accept / abort / manual`` choices.
            instructions_override: Optional custom instructions text to
                replace the default merge-resolution instructions.
            call_file_name: Optional pre-computed filename (including
                extension). When provided, it is used as-is so callers can
                predict the on-disk name (e.g. for embedding in
                ``instructions_override``). When omitted, a name is generated
                from the current timestamp.
            strategy: Optional strategy tier (e.g. ``"strict"``) that produced
                this call file. Consumers can use this to skip the
                ``llm_resolution`` section when it is a placeholder.

        Returns:
            Path to the written call file.
        """
        calls_dir = runtime_dir(self.project_root) / "calls"
        calls_dir.mkdir(parents=True, exist_ok=True)

        if call_file_name:
            call_file = calls_dir / call_file_name
        else:
            call_file = calls_dir / _generate_call_filename(
                "merge", context.theirs_branch,
            )

        # Build file entries with both context and resolution
        file_entries = []
        resolution_files = resolution.files if resolution is not None else []
        context_paths = {cf.path for cf in context.files}
        for cf in context.files:
            # Find matching resolution file
            res_file = None
            for rf in resolution_files:
                if rf.path == cf.path:
                    res_file = rf
                    break

            entry: dict = {
                "path": cf.path,
                "is_spec": cf.is_spec,
                "is_binary": cf.is_binary,
                "hunks": [
                    {"start_line": h.start_line, "end_line": h.end_line}
                    for h in cf.hunks
                ],
                "base_content": cf.base_content if not cf.is_binary else "[binary]",
                "ours_content": cf.ours_content if not cf.is_binary else "[binary]",
                "theirs_content": cf.theirs_content if not cf.is_binary else "[binary]",
                "working_content": cf.working_content if not cf.is_binary else "[binary]",
            }

            if res_file:
                entry["llm_resolution"] = {
                    "resolved_content": res_file.resolved_content if not cf.is_binary else "[binary]",
                    "overall_confidence": res_file.overall_confidence.value,
                    "hunks": [
                        {
                            "start_line": h.start_line,
                            "end_line": h.end_line,
                            "confidence": h.confidence.value,
                            "reasoning": h.reasoning,
                        }
                        for h in res_file.hunks
                    ],
                    "flags": res_file.flags,
                }
            else:
                entry["llm_resolution"] = None

            file_entries.append(entry)

        # Detect orphan files: resolution files not present in context.files.
        # These must pass guardrails before being included (fixes defect F3).
        orphan_files = []
        orphan_guardrails_violations = []
        rejected_orphans: list[dict] = []
        # K3: Guard against empty ours_head_sha — without a pre-merge base
        # ref, the orphan guardrails check compares against HEAD, which
        # after a merge commit would be the merge commit itself, making
        # the check tautological.
        _has_base_ref = bool(context.ours_head_sha)
        for rf in resolution_files:
            if rf.path not in context_paths:
                # F3 (extended): every orphan must be validated, not just
                # spec paths. A buggy or malicious LLM can smuggle
                # arbitrary content into any orphan path that isn't in
                # context.files. Three cases:
                #
                #   1. Spec file with base ref + original present:
                #      run guardrail diff (existing behavior).
                #   2. Spec file with no base ref or no original:
                #      reject outright — we cannot validate.
                #   3. Non-spec file (any state):
                #      reject outright — non-spec orphan writes are
                #      smuggled content, the LLM should never invent
                #      paths outside the conflict context.
                #
                # Rejected orphans are reported to the human reviewer
                # via ``rejected_orphans`` so the operator can see what
                # the LLM tried to write.
                is_spec = _is_spec_path(rf.path)
                if is_spec and _has_base_ref:
                    original = _read_original_for_orphan(
                        self.project_root, rf.path, context.ours_head_sha,
                    )
                    if original is not None:
                        from .guardrails import check_spec_diff
                        orphan_guardrails_violations.extend(
                            check_spec_diff(
                                original, rf.resolved_content, file_path=rf.path,
                            )
                        )
                        orphan_files.append(rf)
                    else:
                        # Spec file does not exist at base_ref: this is
                        # a brand-new spec the LLM is fabricating.
                        # Reject — guardrails cannot be applied to a
                        # file that has no original counterpart.
                        # F3 (extended): still scan the resolved content
                        # for guardrail-like patterns so the operator
                        # has evidence of what was attempted.
                        suspicious = _scan_orphan_content_for_evidence(
                            rf.path, rf.resolved_content,
                        )
                        rejected_orphans.append({
                            "path": rf.path,
                            "reason": (
                                "spec orphan with no original at base ref "
                                "— cannot run guardrail diff"
                            ),
                            "evidence": suspicious,
                        })
                else:
                    # Non-spec orphan OR spec orphan without a usable
                    # base ref: refuse to pass through.
                    # F3 (extended): even though we cannot run a
                    # diff-based guardrails check (no comparable
                    # original), we still scan the resolved content
                    # for guardrail-style invariant violations so the
                    # operator gets actionable evidence about what
                    # the LLM smuggled into a path it should not
                    # touch (e.g., spec-style SHALL/MUST patterns
                    # written into a non-spec file, conflict markers
                    # left behind, or excessive content sizes).
                    suspicious = _scan_orphan_content_for_evidence(
                        rf.path, rf.resolved_content,
                    )
                    rejected_orphans.append({
                        "path": rf.path,
                        "reason": (
                            "non-spec orphan path"
                            if not is_spec
                            else "spec orphan with no usable base ref"
                        ),
                        "evidence": suspicious,
                    })

        for rf in orphan_files:
            entry = {
                "path": rf.path,
                "is_spec": _is_spec_path(rf.path),
                "is_binary": False,
                "is_orphan": True,
                "hunks": [],
                "llm_resolution": {
                    "resolved_content": rf.resolved_content,
                    "overall_confidence": rf.overall_confidence.value,
                    "hunks": [
                        {
                            "start_line": h.start_line,
                            "end_line": h.end_line,
                            "confidence": h.confidence.value,
                            "reasoning": h.reasoning,
                        }
                        for h in rf.hunks
                    ],
                    "flags": rf.flags,
                },
            }
            file_entries.append(entry)

        if resolution is not None:
            llm_overall_confidence = resolution.overall_confidence.value
            llm_flags = resolution.flags
        else:
            llm_overall_confidence = "low"
            llm_flags = {}

        call_data: dict = {
            "type": "merge_conflict",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ours_branch": context.ours_branch,
            "theirs_branch": context.theirs_branch,
            "merge_base": context.merge_base,
            "ours_head_sha": context.ours_head_sha,
            "theirs_head_sha": context.theirs_head_sha,
            "decision_reason": redact_text(decision.reason),
            "files": file_entries,
            "llm_overall_confidence": llm_overall_confidence,
            "llm_flags": llm_flags,
            "options": options or {
                "accept": "Accept LLM resolution — write resolved content and complete merge",
                "abort": "Abort merge — run `git merge --abort` and stop",
                "manual": "Resolve manually — edit files, then run `git add . && git commit`",
            },
            "instructions": instructions_override or (
                f"Merge conflict in {context.theirs_branch} → {context.ours_branch}. "
                f"Review the LLM-proposed resolutions below and choose an option. "
                f"To respond, create a file named '{call_file.name}.response' "
                f"in the same directory with JSON: {{\"choice\": \"accept|abort|manual\", "
                f"\"feedback\": \"optional notes\"}}. "
                f"For 'accept': write the resolved content to files, then run "
                f"`git add . && git commit`. "
                f"For 'abort': run `git merge --abort`. "
                f"For 'manual': edit files to resolve, then run `git add . && git commit`."
            ),
        }
        if strategy is not None:
            call_data["strategy"] = strategy

        if orphan_guardrails_violations:
            call_data["orphan_guardrails_violations"] = [
                {
                    "file_path": v.file_path,
                    "violation_type": v.violation_type,
                    "message": v.message,
                    "evidence": v.evidence,
                }
                for v in orphan_guardrails_violations
            ]

        # F3 (extended): record rejected orphan writes so the operator
        # can see what the LLM tried to smuggle through.  Rejected
        # orphans are NOT added to ``files`` (file_entries) because the
        # call file should not advertise unverifiable content as
        # actionable, but they are surfaced here for audit and review.
        if rejected_orphans:
            call_data["rejected_orphans"] = rejected_orphans

        _atomic_write_json(call_file, call_data)
        logger.info("Created merge human call file: %s", call_file)

        return call_file

    def write_guardrail_call(
        self,
        branch: str,
        violations: list[dict],
        pre_merge_sha: str,
        call_type: str = "guardrail_violation",
        iteration_count: Optional[int] = None,
    ) -> Path:
        """Write a human call file for a guardrail violation after merge.

        Args:
            branch: The branch that was being merged when the violation was detected.
            violations: List of violation dicts with file_path, violation_type, message.
            pre_merge_sha: The SHA of HEAD before the merge (for rollback).
            call_type: Type label for the call file. Defaults to
                ``"guardrail_violation"``; use ``"guardrail_repair_stalled"``
                when the fast-mode repair loop made no progress.
            iteration_count: Number of repair iterations attempted before
                escalation. Only included when non-None.

        Returns:
            Path to the written call file.

        Raises:
            TypeError: If a violation is not a dict.
            ValueError: If a violation dict is missing required keys
                (``file_path``, ``violation_type``, ``message``).
                This is a hard failure — downstream consumers must never
                receive ``<unknown>`` placeholders (fixes defect F4).
        """
        calls_dir = runtime_dir(self.project_root) / "calls"
        calls_dir.mkdir(parents=True, exist_ok=True)

        call_file = calls_dir / _generate_call_filename(
            "merge", f"{branch}_guardrail",
        )

        # Build type-specific instructions so the human knows whether the
        # LLM already attempted repairs.
        if call_type in ("guardrail_repair_stalled", "guardrail_repair_exhausted"):
            if call_type == "guardrail_repair_stalled":
                repair_note = (
                    f"LLM repair was attempted {iteration_count} time(s) but could not "
                    f"reduce the violations — the repair loop stalled. "
                )
            else:
                repair_note = (
                    f"LLM repair was attempted {iteration_count} time(s) but "
                    f"exhausted the maximum allowed iterations without resolving "
                    f"all violations. "
                )
            instructions = (
                f"Guardrail violations detected after merging '{branch}'. "
                f"{repair_note}"
                f"The merge has been rolled back via `git reset --hard {pre_merge_sha}`. "
                f"Review the violations below and choose how to proceed. "
                f"To respond, create a file named '{call_file.name}.response' "
                f"in the same directory with JSON: {{\"choice\": \"accept|abort|manual\", "
                f"\"feedback\": \"optional notes\"}}. "
                f"For 'accept': fix the spec files manually, then re-run `luo merge`. "
                f"For 'abort': no further action needed, the rollback is complete. "
                f"For 'manual': inspect and fix the spec files, then re-run `luo merge`."
            )
        else:
            instructions = (
                f"Guardrail violations detected after merging '{branch}'. "
                f"The merge has been rolled back via `git reset --hard {pre_merge_sha}`. "
                f"Review the violations below and choose how to proceed. "
                f"To respond, create a file named '{call_file.name}.response' "
                f"in the same directory with JSON: {{\"choice\": \"accept|abort|manual\", "
                f"\"feedback\": \"optional notes\"}}. "
                f"For 'accept': fix the spec files manually, then re-run `luo merge`. "
                f"For 'abort': no further action needed, the rollback is complete. "
                f"For 'manual': inspect and fix the spec files, then re-run `luo merge`."
            )

        # Defensive: validate required keys in violation dicts so the call
        # file JSON does not silently carry None/missing values.
        # Missing required keys now raise instead of substituting '<unknown>'
        # (fixes defect F4).
        validated_violations: list[dict] = []
        for v in violations:
            if not isinstance(v, dict):
                raise TypeError(
                    f"write_guardrail_call: expected dict violation, got "
                    f"{type(v).__name__}: {v!r}"
                )
            missing_keys = [
                k for k in ("file_path", "violation_type", "message") if k not in v
            ]
            if missing_keys:
                raise ValueError(
                    f"write_guardrail_call: violation dict missing required "
                    f"keys {missing_keys}: {v!r}"
                )
            validated_violations.append({
                "file_path": v["file_path"],
                "violation_type": v["violation_type"],
                "message": v["message"],
                **{k: v[k] for k in v if k not in ("file_path", "violation_type", "message")},
            })

        call_data: dict = {
            "type": call_type,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "branch": branch,
            "pre_merge_sha": pre_merge_sha,
            "violations": validated_violations,
            "options": {
                "accept": "Accept the merge despite guardrail violations — run `git reset --hard` rollback has already been done; manually fix spec and re-merge",
                "abort": "Keep the rollback — the merge has been aborted and working tree restored to pre-merge state",
                "manual": "Resolve manually — inspect the violations, fix the spec files, then re-run the merge",
            },
            "instructions": instructions,
        }
        if iteration_count is not None:
            call_data["iteration_count"] = iteration_count

        _atomic_write_json(call_file, call_data)
        logger.info("Created guardrail human call file: %s", call_file)

        return call_file

    def print_instructions(self, call_file: Path) -> None:
        """Print user-facing instructions for responding to the call."""
        try:
            call_data = json.loads(call_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            # Display-path only: a missing or malformed call file should
            # degrade gracefully to the empty-context branch below rather
            # than abort instruction printing.  Narrow exception list per
            # the "no silent except Exception" gate — anything else
            # (KeyboardInterrupt, MemoryError, programmer bugs) propagates.
            call_data = {}
        call_type = call_data.get("type", "merge_conflict")

        print(f"\n{'=' * 60}")
        if call_type in ("guardrail_violation", "guardrail_repair_stalled", "guardrail_repair_exhausted"):
            print("  Human review required for guardrail violation")
        else:
            print("  Human review required for merge conflict")
        print(f"{'=' * 60}")
        print(f"\nCall file: {call_file}")
        print(f"\nTo respond, create: {call_file}.response")
        print("\nWith JSON content:")
        print('  {"choice": "accept|abort|manual", "feedback": "notes"}')
        if call_type in ("guardrail_violation", "guardrail_repair_stalled", "guardrail_repair_exhausted"):
            print("\nNext steps:")
            print("  - Review the guardrail violations below")
            print("  - Fix the spec files manually")
            print("  - Re-run: luo merge <branch>")
            violations = call_data.get("violations", [])
            for v in violations[:2]:
                print(f"\n  [{v.get('violation_type', 'UNKNOWN')}] {v.get('file_path', '')}")
                msg = v.get("message", "")
                if msg:
                    print(f"    Message: {msg}")
                evidence = v.get("evidence")
                if evidence:
                    if "strong_line" in evidence and "weak_line" in evidence:
                        print(f"    Strong:  {evidence['strong_line']}")
                        print(f"    Weak:    {evidence['weak_line']}")
                        print(f"    Score:   {evidence.get('pairing_score', 'N/A')}")
                        if "all_pairings" in evidence:
                            ap = evidence["all_pairings"]
                            if len(ap) > 1:
                                print(f"    Additional pairings ({len(ap) - 1}):")
                                for p in ap[1:]:
                                    print(f"      - '{p['strong_line']}' -> '{p['weak_line']}'")
                    elif "deleted_line" in evidence:
                        print(f"    Deleted: {evidence['deleted_line']}")
                    if "when_clauses" in evidence:
                        clauses = evidence["when_clauses"]
                        print(f"    Deleted scenarios ({len(clauses)}):")
                        for wc in clauses[:2]:
                            print(f"      - {wc}")
                        if len(clauses) > 2:
                            print(f"      ... and {len(clauses) - 2} more")
            if len(violations) > 2:
                print(f"\n  ... and {len(violations) - 2} more violation(s)")
        else:
            print("\nThen resolve manually:")
            print("  - Edit files to resolve conflicts")
            print("  - Run: git add . && git commit  (to complete)")
            print("  - Or run: git merge --abort      (to abort)")
        print(f"{'=' * 60}\n")
