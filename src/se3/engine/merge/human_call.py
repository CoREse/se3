"""HumanCallWriter — Create MCP human call files for merge conflicts.

Writes structured call files to ``se3/calls/`` when LLM conflict resolution
triggers a HUMAN_CALL decision. The call file contains conflict context,
LLM resolution proposals, and decision options for human review.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import subprocess
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .conflict_context import ConflictContext
from .conflict_resolver import LLMResolution
from .strategy import StrategyDecision

logger = logging.getLogger(__name__)

# Module-level atomic sequence counter for unique filenames within a process.
# Together with pid and microsecond timestamp, guarantees no collision even
# under millisecond-level concurrency (fixes defect F1).
_call_seq = itertools.count()


def _generate_call_filename(prefix: str, branch: str) -> str:
    """Generate a collision-resistant call filename.

    Format::

        <prefix>_<utc_iso>_<pid>_<seq>_<sha8>_<safe_branch>.json

    Uses UTC timestamp with microsecond precision, process ID, an atomic
    sequence counter, and an 8-char SHA256 hash for additional entropy.
    Even 100 concurrent calls within the same millisecond are guaranteed
    unique because ``seq`` is an atomic process-level counter.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
    pid = os.getpid()
    seq = next(_call_seq)
    hash_input = f"{branch}:{ts}:{pid}:{seq}"
    sha8 = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()[:8]
    safe_branch = branch.replace("/", "__")
    return f"{prefix}_{ts}_{pid}_{seq}_{sha8}_{safe_branch}.json"


def _atomic_write_json(call_file: Path, call_data: dict) -> None:
    """Atomically write JSON call data to ``call_file``.

    Uses a temporary file in the same directory + ``os.replace`` so that
    readers never observe a partially-written file.  Includes fsync on
    the file descriptor and parent directory to survive power loss
    (fixes defect F2).
    """
    call_file.parent.mkdir(parents=True, exist_ok=True)
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
        os.replace(tmp_path, call_file)
        # fsync directory to ensure the rename is durable
        try:
            dir_fd = os.open(str(call_file.parent), os.O_RDONLY | os.O_DIRECTORY)
            os.fsync(dir_fd)
        except OSError:
            pass  # directory fsync is best-effort on some filesystems
    except Exception:
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
    """Return True when ``path`` matches ``se3/specs/**/spec.md``."""
    import re
    return bool(re.match(r"^se3/specs/.+/spec\.md$", path.replace("\\", "/")))


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
        calls_dir = self.project_root / "se3" / "calls"
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
        for rf in resolution_files:
            if rf.path not in context_paths:
                orphan_files.append(rf)
                if _is_spec_path(rf.path):
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
            "decision_reason": decision.reason,
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
        calls_dir = self.project_root / "se3" / "calls"
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
                f"For 'accept': fix the spec files manually, then re-run `se3 merge`. "
                f"For 'abort': no further action needed, the rollback is complete. "
                f"For 'manual': inspect and fix the spec files, then re-run `se3 merge`."
            )
        else:
            instructions = (
                f"Guardrail violations detected after merging '{branch}'. "
                f"The merge has been rolled back via `git reset --hard {pre_merge_sha}`. "
                f"Review the violations below and choose how to proceed. "
                f"To respond, create a file named '{call_file.name}.response' "
                f"in the same directory with JSON: {{\"choice\": \"accept|abort|manual\", "
                f"\"feedback\": \"optional notes\"}}. "
                f"For 'accept': fix the spec files manually, then re-run `se3 merge`. "
                f"For 'abort': no further action needed, the rollback is complete. "
                f"For 'manual': inspect and fix the spec files, then re-run `se3 merge`."
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
        except Exception:
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
            print("  - Re-run: se3 merge <branch>")
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
