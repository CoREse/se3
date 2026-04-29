"""HumanCallWriter — Create MCP human call files for merge conflicts.

Writes structured call files to ``se3/calls/`` when LLM conflict resolution
triggers a HUMAN_CALL decision. The call file contains conflict context,
LLM resolution proposals, and decision options for human review.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from .conflict_context import ConflictContext
from .conflict_resolver import LLMResolution
from .strategy import StrategyDecision

logger = logging.getLogger(__name__)


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
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            safe_branch = context.theirs_branch.replace("/", "-")
            call_file = calls_dir / f"merge_{ts}_{safe_branch}.json"

        # Build file entries with both context and resolution
        file_entries = []
        resolution_files = resolution.files if resolution is not None else []
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

        if resolution is not None:
            llm_overall_confidence = resolution.overall_confidence.value
            llm_flags = resolution.flags
        else:
            llm_overall_confidence = "low"
            llm_flags = {}

        call_data: dict = {
            "type": "merge_conflict",
            "created_at": datetime.now().isoformat(),
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

        call_file.write_text(
            json.dumps(call_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Created merge human call file: %s", call_file)

        return call_file

    def write_guardrail_call(
        self,
        branch: str,
        violations: list[dict],
        pre_merge_sha: str,
    ) -> Path:
        """Write a human call file for a guardrail violation after merge.

        Args:
            branch: The branch that was being merged when the violation was detected.
            violations: List of violation dicts with file_path, violation_type, message.
            pre_merge_sha: The SHA of HEAD before the merge (for rollback).

        Returns:
            Path to the written call file.
        """
        calls_dir = self.project_root / "se3" / "calls"
        calls_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_branch = branch.replace("/", "-")
        call_file = calls_dir / f"merge_{ts}_{safe_branch}_guardrail.json"

        call_data = {
            "type": "guardrail_violation",
            "created_at": datetime.now().isoformat(),
            "branch": branch,
            "pre_merge_sha": pre_merge_sha,
            "violations": violations,
            "options": {
                "accept": "Accept the merge despite guardrail violations — run `git reset --hard` rollback has already been done; manually fix spec and re-merge",
                "abort": "Keep the rollback — the merge has been aborted and working tree restored to pre-merge state",
                "manual": "Resolve manually — inspect the violations, fix the spec files, then re-run the merge",
            },
            "instructions": (
                f"Guardrail violations detected after merging '{branch}'. "
                f"The merge has been rolled back via `git reset --hard {pre_merge_sha}`. "
                f"Review the violations below and choose how to proceed. "
                f"To respond, create a file named '{call_file.name}.response' "
                f"in the same directory with JSON: {{\"choice\": \"accept|abort|manual\", "
                f"\"feedback\": \"optional notes\"}}. "
                f"For 'accept': fix the spec files manually, then re-run `se3 merge`. "
                f"For 'abort': no further action needed, the rollback is complete. "
                f"For 'manual': inspect and fix the spec files, then re-run `se3 merge`."
            ),
        }

        call_file.write_text(
            json.dumps(call_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Created guardrail human call file: %s", call_file)

        return call_file

    def print_instructions(self, call_file: Path) -> None:
        """Print user-facing instructions for responding to the call."""
        print(f"\n{'=' * 60}")
        print("  Human review required for merge conflict")
        print(f"{'=' * 60}")
        print(f"\nCall file: {call_file}")
        print(f"\nTo respond, create: {call_file}.response")
        print("\nWith JSON content:")
        print('  {"choice": "accept|abort|manual", "feedback": "notes"}')
        print("\nThen resolve manually:")
        print("  - Edit files to resolve conflicts")
        print("  - Run: git add . && git commit  (to complete)")
        print("  - Or run: git merge --abort      (to abort)")
        print(f"{'=' * 60}\n")
