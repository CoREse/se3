"""GuardrailRepairer — LLM-driven spec repair for fast-mode guardrail violations.

When post-merge guardrails detect violations in ``fast`` strategy, this module
constructs a repair prompt, calls the LLM, parses the corrected spec content,
writes it back to the working tree, amends the merge commit, and re-runs the
guardrails check to verify the fix.

All write-back paths are restricted to ``se3/specs/**/spec.md``.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..json_extractor import extract_json_two_phase
from ..worktree import _run_git
from .guardrails import GuardrailViolation, MergeGuardrailsCheck, _is_spec_path

logger = logging.getLogger(__name__)

_REPAIR_SCHEMA = """{
  "files": [
    {
      "path": "relative/path/to/spec.md",
      "corrected_content": "full corrected file content with no conflict markers"
    }
  ]
}"""

_REPAIR_PROMPT_TEMPLATE = """You are a spec-file corrector. A merge commit introduced guardrail violations in spec files. Your task is to fix the violations and return the corrected spec file contents as JSON.

## Guardrail Violations Detected

{violations_text}

## Original Spec Files (before merge)

{original_specs_text}

## Merged Spec Files (after merge — these contain violations)

{merged_specs_text}

## Rules

1. You MUST restore any weakened requirements:
   - SHALL → SHOULD: change back to SHALL
   - MUST → SHOULD: change back to MUST
   - REQUIRED → RECOMMENDED/OPTIONAL: change back to REQUIRED
   - all → some: change back to all
   - every → some: change back to every
2. You MUST restore any deleted WHEN clauses (scenarios).
3. You MUST NOT invent new requirements that did not exist in the original.
4. You MUST NOT delete requirements that existed in the original.
5. Preserve all other changes from the merge that do not violate guardrails.
6. Return ONLY a valid JSON object matching this schema (no markdown fences, no prose):

{schema}

Respond with valid JSON only:"""


@dataclass
class RepairResult:
    """Result of a guardrail repair attempt."""

    success: bool = False
    repaired_files: list[str] = field(default_factory=list)
    error: Optional[str] = None


class GuardrailRepairer:
    """Repair guardrail violations in spec files via LLM.

    Used in ``fast`` strategy when post-merge guardrails detect violations.
    The repairer sends the violation list + spec contents to the LLM, receives
    corrected file contents, writes them back, amends the merge commit, and
    re-runs guardrails to verify.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def repair_violations(
        self,
        branch: str,
        pre_sha: str,
        post_sha: str,
        violations: list[GuardrailViolation],
        original_spec_contents: dict[str, str],
        merged_spec_contents: dict[str, str],
    ) -> RepairResult:
        """Attempt to repair guardrail violations via LLM.

        Args:
            branch: The branch being merged (for logging).
            pre_sha: SHA of HEAD before the merge.
            post_sha: SHA of the merge commit.
            violations: List of detected GuardrailViolation objects.
            original_spec_contents: Dict mapping spec file paths to their
                original content (from pre_sha).
            merged_spec_contents: Dict mapping spec file paths to their
                merged content (from post_sha / working tree).

        Returns:
            RepairResult: success=True if guardrails pass after repair,
            success=False with error text otherwise.
        """
        # Build violation text
        violations_text = self._format_violations(violations)

        # Build original/merged spec text
        original_specs_text = self._format_spec_contents(original_spec_contents)
        merged_specs_text = self._format_spec_contents(merged_spec_contents)

        # Build and send repair prompt
        prompt = _REPAIR_PROMPT_TEMPLATE.format(
            violations_text=violations_text,
            original_specs_text=original_specs_text,
            merged_specs_text=merged_specs_text,
            schema=_REPAIR_SCHEMA,
        )

        try:
            raw_response = self._call_llm(prompt)
        except Exception as exc:
            logger.warning("Guardrail repair LLM call failed: %s", exc)
            return RepairResult(
                success=False,
                error=f"LLM call failed: {exc}",
            )

        if raw_response == "" or raw_response is None:
            return RepairResult(
                success=False,
                error="LLM returned empty response for guardrail repair",
            )
        if raw_response == {} or raw_response == {"files": []}:
            return RepairResult(
                success=False,
                error="LLM returned empty response (no files to repair)",
            )

        # Parse LLM response
        parsed = self._parse_response(raw_response)
        if parsed is None:
            return RepairResult(
                success=False,
                error="Failed to parse LLM repair response as JSON",
            )

        # Write corrected files back
        files_data = parsed.get("files", [])
        if not isinstance(files_data, list):
            return RepairResult(
                success=False,
                error=f"LLM repair response 'files' is not a list: {type(files_data).__name__}",
            )

        # Build the set of spec files that were actually changed in the merge
        allowed_paths = set(original_spec_contents.keys()) | set(merged_spec_contents.keys())

        repaired_files: list[str] = []
        skipped_missing_content: list[str] = []
        for file_data in files_data:
            if not isinstance(file_data, dict):
                continue

            path = file_data.get("path", "")
            corrected_content = file_data.get("corrected_content", "")

            if not path:
                continue

            # Skip entries without corrected content (missing or empty)
            if not corrected_content:
                skipped_missing_content.append(path)
                continue

            # Resolve full path and validate it lies inside se3/specs/
            full_path = (self.project_root / path).resolve()
            specs_dir = (self.project_root / "se3" / "specs").resolve()
            try:
                full_path.relative_to(specs_dir)
            except ValueError:
                logger.warning(
                    "Guardrail repair attempted to write outside spec dir: %s — rejected",
                    path,
                )
                self._restore_merged_content(repaired_files, merged_spec_contents)
                return RepairResult(
                    success=False,
                    error=f"Guardrail repair attempted to write outside spec dir: {path}",
                )

            # Must also match the spec file naming pattern
            if not _is_spec_path(path):
                logger.warning(
                    "Guardrail repair attempted to write non-spec path: %s — rejected",
                    path,
                )
                self._restore_merged_content(repaired_files, merged_spec_contents)
                return RepairResult(
                    success=False,
                    error=f"Guardrail repair attempted to write non-spec path: {path}",
                )

            # Defense-in-depth: reject files not among the changed spec set.
            # An empty allowed_paths set means the caller passed no spec contents,
            # which is a bug or race condition — reject to prevent hallucinated paths.
            if not allowed_paths or path not in allowed_paths:
                logger.warning(
                    "Guardrail repair attempted to write unexpected spec file: %s — "
                    "not in the changed spec set (%s) — rejected",
                    path,
                    ", ".join(sorted(allowed_paths)),
                )
                self._restore_merged_content(repaired_files, merged_spec_contents)
                return RepairResult(
                    success=False,
                    error=(
                        f"Guardrail repair attempted to write unexpected spec file: "
                        f"{path}. Only changed spec files may be repaired."
                    ),
                )

            # Defense-in-depth: reject content that still contains conflict markers
            if "<<<<<<<" in corrected_content or ">>>>>>>" in corrected_content:
                logger.warning(
                    "Guardrail repair rejected corrected content for %s: "
                    "contains unresolved git conflict markers",
                    path,
                )
                self._restore_merged_content(repaired_files, merged_spec_contents)
                return RepairResult(
                    success=False,
                    error=(
                        f"Guardrail repair rejected corrected content for {path}: "
                        f"contains unresolved git conflict markers"
                    ),
                )

            # Write corrected content
            try:
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(corrected_content, encoding="utf-8")
                repaired_files.append(path)
                logger.info("Guardrail repair wrote corrected spec: %s", path)
            except Exception as exc:
                self._restore_merged_content(repaired_files, merged_spec_contents)
                return RepairResult(
                    success=False,
                    error=f"Failed to write repaired spec {path}: {exc}",
                )

        if not repaired_files:
            if skipped_missing_content:
                return RepairResult(
                    success=False,
                    error=(
                        f"LLM repair returned {len(skipped_missing_content)} file entry(s) "
                        f"without corrected_content: {', '.join(skipped_missing_content)}"
                    ),
                )
            return RepairResult(
                success=False,
                error="LLM repair returned no valid spec files to write",
            )

        # Stage, amend, and re-check — catch timeouts so the orchestrator
        # knows the failure mode precisely instead of misattributing it as a
        # guardrails-check crash.
        amend_succeeded = False
        try:
            # Stage repaired files
            for path in repaired_files:
                add_result = _run_git(
                    self.project_root, "add", path,
                    check=False, timeout=15,
                )
                if add_result.returncode != 0:
                    # Unstage any previously staged files before restoring
                    for staged_path in repaired_files:
                        _run_git(
                            self.project_root, "reset", "HEAD", staged_path,
                            check=False, timeout=15,
                        )
                    self._restore_merged_content(repaired_files, merged_spec_contents)
                    return RepairResult(
                        success=False,
                        error=f"Failed to stage repaired spec {path}: {add_result.stderr.strip()}",
                    )

            # Amend the merge commit with repaired specs
            amend_result = _run_git(
                self.project_root,
                "commit",
                "--amend",
                "--no-edit",
                check=False, timeout=30,
            )
            if amend_result.returncode != 0:
                # Unstage repaired files so the repairer is self-contained
                for path in repaired_files:
                    _run_git(
                        self.project_root, "reset", "HEAD", path,
                        check=False, timeout=15,
                    )
                self._restore_merged_content(repaired_files, merged_spec_contents)
                return RepairResult(
                    success=False,
                    error=f"Failed to amend merge commit with repaired specs: {amend_result.stderr.strip()}",
                )

            amend_succeeded = True
            logger.info("Amended merge commit with repaired spec files for '%s'", branch)

            # Re-run guardrails on the amended commit
            guardrails = MergeGuardrailsCheck(self.project_root)
            rev_parse_result = _run_git(
                self.project_root, "rev-parse", "HEAD",
                check=False, timeout=15,
            )
            if rev_parse_result.returncode != 0:
                self._restore_merged_content(repaired_files, merged_spec_contents)
                return RepairResult(
                    success=False,
                    error=f"Failed to get amended commit SHA: {rev_parse_result.stderr.strip() or 'git rev-parse HEAD failed'}",
                )
            amended_sha = rev_parse_result.stdout.strip()
            gr_report = guardrails.check_merge_result(pre_sha, amended_sha)
        except subprocess.TimeoutExpired as exc:
            # Defensive: un-amend the commit before restoring working-tree
            # files so that if the process crashes before the caller's
            # rollback, HEAD won't be left on an unverified amended commit.
            # Only un-amend if the amend actually succeeded; otherwise we
            # would accidentally undo the original merge commit.
            if amend_succeeded:
                reset_result = _run_git(
                    self.project_root, "reset", "--soft", "HEAD~1",
                    check=False, timeout=15,
                )
                if reset_result.returncode != 0:
                    # TODO: A failed un-amend leaves HEAD on an unverified amended
                    # commit. The orchestrator's subsequent _rollback_to(pre_sha)
                    # will move HEAD regardless, masking this failure. If the
                    # orchestrator were to return success unexpectedly, the repo
                    # would be left in an inconsistent state. Consider raising
                    # here instead of only logging.
                    logger.warning(
                        "Un-amend (reset --soft HEAD~1) failed after timeout: %s",
                        reset_result.stderr.strip() or "unknown error",
                    )
                # Unstage repaired files so index and working tree stay in sync
                for path in repaired_files:
                    _run_git(
                        self.project_root, "reset", "HEAD", path,
                        check=False, timeout=15,
                    )
            self._restore_merged_content(repaired_files, merged_spec_contents)
            return RepairResult(
                success=False,
                error=f"Timeout during guardrail repair git operation: {exc}",
            )
        except Exception as exc:
            # Defensive: un-amend the commit (same reasoning as above).
            if amend_succeeded:
                reset_result = _run_git(
                    self.project_root, "reset", "--soft", "HEAD~1",
                    check=False, timeout=15,
                )
                if reset_result.returncode != 0:
                    # TODO: See matching TODO above in the TimeoutExpired path.
                    logger.warning(
                        "Un-amend (reset --soft HEAD~1) failed after exception: %s",
                        reset_result.stderr.strip() or "unknown error",
                    )
            self._restore_merged_content(repaired_files, merged_spec_contents)
            return RepairResult(
                success=False,
                error=f"Guardrails re-check failed after repair: {exc}",
            )

        if not gr_report.passed:
            # Still has violations after repair — restore and report failure.
            # Reorder: (a) soft-reset to undo the amend, (b) unstage repaired
            # files from the new HEAD so index and working tree stay in sync,
            # (c) restore the original merged content.  This makes the repairer
            # self-contained regardless of whether the caller performs a
            # downstream hard reset.
            if amend_succeeded:
                _run_git(
                    self.project_root, "reset", "--soft", "HEAD~1",
                    check=False, timeout=15,
                )
                for path in repaired_files:
                    _run_git(
                        self.project_root, "reset", "HEAD", path,
                        check=False, timeout=15,
                    )
            self._restore_merged_content(repaired_files, merged_spec_contents)
            remaining = [
                f"[{v.violation_type}] {v.file_path}: {v.message}"
                for v in gr_report.violations
            ]
            return RepairResult(
                success=False,
                repaired_files=repaired_files,
                error=(
                    f"Guardrails still fail after LLM repair. "
                    f"Remaining violations ({len(gr_report.violations)}): "
                    f"{'; '.join(remaining)}"
                ),
            )

        logger.info(
            "Guardrail repair succeeded for '%s': %d file(s) corrected",
            branch, len(repaired_files),
        )
        return RepairResult(
            success=True,
            repaired_files=repaired_files,
        )

    def _format_violations(self, violations: list[GuardrailViolation]) -> str:
        """Format violation list for the repair prompt."""
        lines: list[str] = []
        for i, v in enumerate(violations, 1):
            lines.append(f"{i}. [{v.violation_type}] {v.file_path}")
            lines.append(f"   {v.message}")
            evidence = v.evidence
            if evidence:
                if "strong_line" in evidence and "weak_line" in evidence:
                    lines.append(
                        f"   Original:  '{evidence['strong_line']}' "
                        f"(line {evidence.get('strong_line_no', '?')})"
                    )
                    lines.append(
                        f"   Modified:  '{evidence['weak_line']}' "
                        f"(line {evidence.get('weak_line_no', '?')})"
                    )
                    if "pairing_score" in evidence:
                        lines.append(
                            f"   Pairing score: {evidence['pairing_score']}"
                        )
                    if "all_pairings" in evidence:
                        ap = evidence["all_pairings"]
                        if len(ap) > 1:
                            lines.append(
                                f"   Additional pairings ({len(ap) - 1}):"
                            )
                            for p in ap[1:]:
                                lines.append(
                                    f"     - '{p['strong_line']}' -> "
                                    f"'{p['weak_line']}' "
                                    f"(line {p.get('strong_line_no', '?')} -> "
                                    f"{p.get('weak_line_no', '?')})"
                                )
                if "deleted_line" in evidence:
                    lines.append(
                        f"   Deleted:   '{evidence['deleted_line']}' "
                        f"(line {evidence.get('deleted_line_no', '?')})"
                    )
                if "when_clauses" in evidence:
                    for wc in evidence["when_clauses"]:
                        lines.append(f"   Deleted WHEN: '{wc}'")
                # Defensive fallback: dump any unrecognized evidence keys so
                # the LLM repair prompt still gets context when evidence shape
                # evolves (e.g. new detector adds a novel key).
                # Use str() for scalars and json.dumps() for collections so
                # the prompt format stays consistent with the recognized branches.
                recognized = {
                    "strong_line", "weak_line", "strong_line_no",
                    "weak_line_no", "pairing_score", "deleted_line",
                    "deleted_line_no", "when_clauses", "all_pairings",
                }
                for key, value in evidence.items():
                    if key not in recognized:
                        if isinstance(value, (list, dict, tuple, set)):
                            lines.append(f"   {key}: {json.dumps(value, ensure_ascii=False)}")
                        else:
                            lines.append(f"   {key}: {str(value)}")
        return "\n".join(lines) if lines else "(none)"

    def _format_spec_contents(self, contents: dict[str, str]) -> str:
        """Format spec contents dict for the repair prompt."""
        lines: list[str] = []
        for path, content in sorted(contents.items()):
            lines.append(f"--- {path} ---")
            lines.append(content)
            lines.append("")
        return "\n".join(lines) if lines else "(none)"

    def _restore_merged_content(
        self,
        repaired_files: list[str],
        merged_spec_contents: dict[str, str],
    ) -> None:
        """Restore the merged spec content for files that were modified.

        Called when repair fails after files have been written, to leave the
        working tree in the same state it was before ``repair_violations`` was
        called.  This makes the repairer self-contained: callers do not have
        to perform their own cleanup.
        """
        for path in repaired_files:
            merged_content = merged_spec_contents.get(path)
            if merged_content is not None:
                full_path = (self.project_root / path).resolve()
                try:
                    full_path.write_text(merged_content, encoding="utf-8")
                    logger.info("Restored merged content for %s", path)
                except Exception as exc:
                    logger.warning(
                        "Failed to restore merged content for %s: %s",
                        path, exc,
                    )
            else:
                logger.warning(
                    "No merged content available to restore for %s", path,
                )

    def _call_llm(self, prompt: str) -> str | dict[str, Any]:
        """Call LLM with the repair prompt."""
        from ..llm_caller import LLMCaller

        caller = LLMCaller(
            project_root=self.project_root,
            step_type="guardrail_repair",
            max_retries=2,
            retry_delay=1.0,
        )
        # Use two-phase extraction for robust JSON parsing
        raw = caller.call(
            prompt=prompt,
            require_json=False,
        )

        # Try to extract JSON using two-phase extraction
        schema_hint = (
            "A JSON object with a 'files' array. Each file has 'path' and "
            "'corrected_content' fields."
        )
        try:
            parsed = extract_json_two_phase(
                raw,
                project_root=self.project_root,
                schema_hint=schema_hint,
                required_keys=["files"],
            )
        except Exception as exc:
            logger.warning(
                "Guardrail repair LLM response parsing failed: %s", exc
            )
            raise ValueError(
                f"guardrail repair LLM response parsing failed: {exc}"
            ) from exc

        if parsed is not None:
            # Return the dict directly to avoid a wasteful
            # serialize→deserialize round-trip.
            return parsed

        # Return raw for fallback parsing
        return raw

    def _parse_response(self, raw_response: str | dict[str, Any]) -> Optional[dict[str, Any]]:
        """Parse LLM repair response as JSON dict."""
        if isinstance(raw_response, dict):
            if "files" in raw_response:
                return raw_response
            return None

        from ..utils.json_parser import parse_json_response

        return parse_json_response(raw_response, required_keys=["files"])
