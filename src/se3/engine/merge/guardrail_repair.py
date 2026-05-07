"""GuardrailRepairer — LLM-driven spec repair for fast-mode guardrail violations.

When post-merge guardrails detect violations in ``fast`` strategy, this module
constructs a repair prompt, calls the LLM, parses the corrected spec content,
writes it back to the working tree, commits the fix (preferring a fix-up
commit on top of the merge commit, with amend as a fallback), and re-runs
the guardrails check to verify the fix.

All write-back paths are restricted to ``se3/specs/**/spec.md``.

**Amend safety contract** (defense against the user-accident root cause A1-A4):
All amend operations MUST save ``pre_amend_sha`` before ``git commit --amend``.
If rollback is needed, ``git reset --soft <pre_amend_sha>`` is used instead of
``git reset --soft HEAD~1``.  The ``HEAD^2`` existence check confirms HEAD is
still a merge commit before amending.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from ..json_extractor import extract_json_two_phase
from ..worktree import _run_git
from ...commands.merge.secret_redact import redact_text
from .guardrails import GuardrailViolation, MergeGuardrailsCheck, _is_spec_path

if TYPE_CHECKING:
    from ..llm_caller import LLMCaller

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
    corrected file contents, writes them back, commits the fix, and re-runs
    guardrails to verify.

    Args:
        project_root: Path to the project root.
        llm_caller: Optional shared :class:`LLMCaller` instance. When provided,
            the repairer reuses it (sharing prompt cache and retry state).
            When ``None``, the repairer creates its own per-call instance
            (legacy behavior).
    """

    def __init__(
        self,
        project_root: Path,
        *,
        llm_caller: Optional["LLMCaller"] = None,
        llm_trace: Optional[Any] = None,
    ) -> None:
        self.project_root = project_root
        self._llm_caller = llm_caller
        self._llm_trace = llm_trace

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
            post_sha: SHA of the merge commit. Used as the safe rollback target
                when the fix-up-commit path is used; callers should refresh
                this if amend is used.
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

        # A7 fix: distinguish None from empty string
        if raw_response is None:
            return RepairResult(
                success=False,
                error="LLM returned None for guardrail repair",
            )
        if raw_response == "" or raw_response == "":
            return RepairResult(
                success=False,
                error="LLM returned empty response for guardrail repair",
            )
        # A7 fix: distinguish {} from None
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
        # A7 fix: distinguish parsed being {} from None
        if parsed == {}:
            return RepairResult(
                success=False,
                error="LLM repair response parsed to empty dict",
            )

        # Write corrected files back
        files_data = parsed.get("files", [])
        if not isinstance(files_data, list):
            return RepairResult(
                success=False,
                error=f"LLM repair response 'files' is not a list: {type(files_data).__name__}",
            )

        # Build the set of spec files that were actually changed in the merge.
        # This set is refreshed after every _restore_merged_content call so
        # that stale reads (defect A8) cannot occur.
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
                # Refresh allowed_paths after restore (A8)
                allowed_paths = set(original_spec_contents.keys()) | set(merged_spec_contents.keys())
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
                allowed_paths = set(original_spec_contents.keys()) | set(merged_spec_contents.keys())
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
                allowed_paths = set(original_spec_contents.keys()) | set(merged_spec_contents.keys())
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
                allowed_paths = set(original_spec_contents.keys()) | set(merged_spec_contents.keys())
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
                allowed_paths = set(original_spec_contents.keys()) | set(merged_spec_contents.keys())
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

        # Stage, commit, and re-check — catch timeouts so the orchestrator
        # knows the failure mode precisely instead of misattributing it as a
        # guardrails-check crash.
        commit_succeeded = False
        pre_amend_sha: Optional[str] = None
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

            # --- PREFERRED PATH: fix-up commit on top of merge commit ---
            # This avoids the amend/reset footgun entirely.  It creates a
            # separate commit that fixes the spec files, leaving the original
            # merge commit intact.
            fixup_result = _run_git(
                self.project_root,
                "commit",
                "-m",
                f"fix(specs): repair guardrail violations from '{branch}'",
                check=False, timeout=30,
            )
            if fixup_result.returncode == 0:
                commit_succeeded = True
                logger.info(
                    "Created fix-up commit for '%s' with %d repaired spec file(s)",
                    branch, len(repaired_files),
                )
            else:
                # --- FALLBACK PATH: amend the merge commit ---
                # This is the legacy path.  Before amending, assert HEAD is
                # still a merge commit, and save pre_amend_sha for safe
                # rollback.
                logger.warning(
                    "Fix-up commit failed (%s), falling back to amend: %s",
                    fixup_result.returncode,
                    fixup_result.stderr.strip() or "unknown error",
                )

                # A5: assert HEAD is still a merge commit before amending
                head_parent2 = _run_git(
                    self.project_root, "rev-parse", "--verify", "HEAD^2",
                    check=False, timeout=15,
                )
                if head_parent2.returncode != 0:
                    # HEAD is no longer a merge commit — cannot safely amend.
                    # Unstage and restore.
                    for path in repaired_files:
                        _run_git(
                            self.project_root, "reset", "HEAD", path,
                            check=False, timeout=15,
                        )
                    self._restore_merged_content(repaired_files, merged_spec_contents)
                    return RepairResult(
                        success=False,
                        error=(
                            f"Cannot amend: HEAD is not a merge commit "
                            f"(HEAD^2 check failed: {head_parent2.stderr.strip()})"
                        ),
                    )

                # Save pre_amend_sha before any amend operation (A1 fix).
                pre_sha_result = _run_git(
                    self.project_root, "rev-parse", "HEAD",
                    check=False, timeout=15,
                )
                if pre_sha_result.returncode != 0:
                    for path in repaired_files:
                        _run_git(
                            self.project_root, "reset", "HEAD", path,
                            check=False, timeout=15,
                        )
                    self._restore_merged_content(repaired_files, merged_spec_contents)
                    return RepairResult(
                        success=False,
                        error="Cannot save pre_amend_sha: git rev-parse HEAD failed",
                    )
                pre_amend_sha = pre_sha_result.stdout.strip()

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

                commit_succeeded = True
                logger.info("Amended merge commit with repaired spec files for '%s'", branch)

            # Re-run guardrails on the commit (fix-up or amended)
            guardrails = MergeGuardrailsCheck(self.project_root)
            rev_parse_result = _run_git(
                self.project_root, "rev-parse", "HEAD",
                check=False, timeout=15,
            )
            if rev_parse_result.returncode != 0:
                self._rollback_commit(pre_amend_sha, repaired_files, merged_spec_contents)
                return RepairResult(
                    success=False,
                    error=f"Failed to get post-repair commit SHA: {rev_parse_result.stderr.strip() or 'git rev-parse HEAD failed'}",
                )
            new_sha = rev_parse_result.stdout.strip()
            gr_report = guardrails.check_merge_result(pre_sha, new_sha)
        except subprocess.TimeoutExpired as exc:
            # Defensive: un-amend the commit before restoring working-tree
            # files so that if the process crashes before the caller's
            # rollback, HEAD won't be left on an unverified commit.
            if commit_succeeded:
                self._rollback_commit(pre_amend_sha, repaired_files, merged_spec_contents)
            else:
                self._restore_merged_content(repaired_files, merged_spec_contents)
            return RepairResult(
                success=False,
                error=f"Timeout during guardrail repair git operation: {exc}",
            )
        except Exception as exc:
            # Defensive: un-amend the commit (same reasoning as above).
            if commit_succeeded:
                self._rollback_commit(pre_amend_sha, repaired_files, merged_spec_contents)
            else:
                self._restore_merged_content(repaired_files, merged_spec_contents)
            return RepairResult(
                success=False,
                error=f"Guardrails re-check failed after repair: {exc}",
            )

        if not gr_report.passed:
            # Still has violations after repair — restore and report failure.
            # Reorder: (a) rollback the commit, (b) unstage repaired
            # files from the new HEAD so index and working tree stay in sync,
            # (c) restore the original merged content.  This makes the repairer
            # self-contained regardless of whether the caller performs a
            # downstream hard reset.
            if commit_succeeded:
                self._rollback_commit(pre_amend_sha, repaired_files, merged_spec_contents)
            else:
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

        # A11 fix / Task 11: after repair success, verify the original merge
        # commit (post_sha) is still an ancestor of HEAD.  This catches the
        # case where the merge was silently lost during the repair process
        # (e.g. amend reset went to the wrong parent).  We use ancestry
        # rather than parent-counting so the check works regardless of
        # whether the repair path created a fix-up commit or amended.
        # The check is skipped when post_sha is not a valid ref (e.g. tests
        # with mocked SHAs) to avoid false failures.
        if post_sha:
            verify_ref = _run_git(
                self.project_root, "rev-parse", "--verify", post_sha,
                check=False, timeout=15,
            )
            if verify_ref.returncode == 0:
                ancestor_check = _run_git(
                    self.project_root,
                    "merge-base", "--is-ancestor", post_sha, "HEAD",
                    check=False, timeout=15,
                )
                if ancestor_check.returncode != 0:
                    self._rollback_commit(pre_amend_sha, repaired_files, merged_spec_contents)
                    return RepairResult(
                        success=False,
                        error=(
                            "Post-repair post-condition failed: the original merge "
                            f"commit ({post_sha[:8]}) is no longer an ancestor of HEAD. "
                            "The merge may have been silently lost."
                        ),
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

        Raises:
            OSError: If a file cannot be written back.  Previously (defect A6)
            this was silently swallowed; now it is re-raised so the caller
            knows the working tree may be inconsistent.
        """
        for path in repaired_files:
            merged_content = merged_spec_contents.get(path)
            if merged_content is not None:
                full_path = (self.project_root / path).resolve()
                try:
                    full_path.write_text(merged_content, encoding="utf-8")
                    logger.info("Restored merged content for %s", path)
                except OSError as exc:
                    logger.error(
                        "Failed to restore merged content for %s: %s",
                        path, exc,
                    )
                    raise  # A6 fix: re-raise, do not swallow
            else:
                logger.warning(
                    "No merged content available to restore for %s", path,
                )

    def _rollback_commit(
        self,
        pre_amend_sha: Optional[str],
        repaired_files: list[str],
        merged_spec_contents: dict[str, str],
    ) -> None:
        """Rollback a commit created by the repair process.

        When the fix-up-commit path was used (pre_amend_sha is None), roll
        back one commit via ``git reset --soft HEAD~1``.

        When the amend path was used (pre_amend_sha is set), reset to
        ``pre_amend_sha`` instead of ``HEAD~1`` so the original merge commit
        is not lost (defect A1-A4 fix).

        After resetting, unstages repaired files and restores merged content.
        """
        if pre_amend_sha:
            # Amend path: rollback to the saved pre-amend SHA.
            reset_result = _run_git(
                self.project_root, "reset", "--soft", pre_amend_sha,
                check=False, timeout=15,
            )
            if reset_result.returncode != 0:
                logger.warning(
                    "Rollback to pre_amend_sha %s failed: %s",
                    pre_amend_sha[:8] if pre_amend_sha else "<none>",
                    reset_result.stderr.strip() or "unknown error",
                )
        else:
            # Fix-up commit path: rollback one commit.
            reset_result = _run_git(
                self.project_root, "reset", "--soft", "HEAD~1",
                check=False, timeout=15,
            )
            if reset_result.returncode != 0:
                logger.warning(
                    "Rollback of fix-up commit (HEAD~1) failed: %s",
                    reset_result.stderr.strip() or "unknown error",
                )

        # Unstage repaired files so index and working tree stay in sync
        for path in repaired_files:
            _run_git(
                self.project_root, "reset", "HEAD", path,
                check=False, timeout=15,
            )

        # Restore merged content
        self._restore_merged_content(repaired_files, merged_spec_contents)

    def _call_llm(self, prompt: str) -> str | dict[str, Any]:
        """Call LLM with the repair prompt.

        Uses the injected :attr:`_llm_caller` if available (Task 12 / A13 fix),
        otherwise falls back to creating a per-call :class:`LLMCaller`.

        K2: If an :class:`LLMTrace` was injected, the call is timed and
        recorded as a jsonl entry.
        """
        caller = self._llm_caller
        if caller is None:
            from ..llm_caller import LLMCaller

            caller = LLMCaller(
                project_root=self.project_root,
                step_type="guardrail_repair",
                max_retries=2,
                retry_delay=1.0,
            )

        t0 = time.monotonic()
        raw: str = ""
        outcome: str = ""
        error: Optional[str] = None
        try:
            raw = caller.call(
                prompt=prompt,
                require_json=False,
            )
            outcome = "success"
        except Exception as exc:
            outcome = "error"
            error = str(exc)
            raise
        finally:
            if self._llm_trace is not None:
                self._llm_trace.record(
                    agent="guardrail_repair",
                    prompt=redact_text(prompt),
                    response=redact_text(raw) if outcome == "success" else "",
                    duration_sec=time.monotonic() - t0,
                    outcome=outcome,
                    error=error,
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
