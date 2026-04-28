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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..worktree import _run_git
from .guardrails import GuardrailViolation, MergeGuardrailsCheck, check_spec_diff

logger = logging.getLogger(__name__)

_SPEC_PATH_RE = re.compile(r"^se3/specs/.+/spec\.md$")

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

        if not raw_response:
            return RepairResult(
                success=False,
                error="LLM returned empty response for guardrail repair",
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

        repaired_files: list[str] = []
        for file_data in files_data:
            if not isinstance(file_data, dict):
                continue

            path = file_data.get("path", "")
            corrected_content = file_data.get("corrected_content", "")

            if not path:
                continue

            # Skip entries without corrected content (missing or empty)
            if not corrected_content:
                continue

            # Validate path is a spec path
            if not self._is_spec_path(path):
                logger.warning(
                    "Guardrail repair attempted to write non-spec path: %s — rejected",
                    path,
                )
                return RepairResult(
                    success=False,
                    error=f"Guardrail repair attempted to write non-spec path: {path}",
                )

            # Resolve full path inside project root
            full_path = (self.project_root / path).resolve()
            try:
                full_path.relative_to(self.project_root.resolve())
            except ValueError:
                return RepairResult(
                    success=False,
                    error=f"Guardrail repair path outside project root: {path}",
                )

            # Write corrected content
            try:
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(corrected_content, encoding="utf-8")
                repaired_files.append(path)
                logger.info("Guardrail repair wrote corrected spec: %s", path)
            except Exception as exc:
                return RepairResult(
                    success=False,
                    error=f"Failed to write repaired spec {path}: {exc}",
                )

        if not repaired_files:
            return RepairResult(
                success=False,
                error="LLM repair returned no valid spec files to write",
            )

        # Stage repaired files
        for path in repaired_files:
            add_result = _run_git(
                self.project_root, "add", path,
                check=False, timeout=15,
            )
            if add_result.returncode != 0:
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
            return RepairResult(
                success=False,
                error=f"Failed to amend merge commit with repaired specs: {amend_result.stderr.strip()}",
            )

        logger.info("Amended merge commit with repaired spec files for '%s'", branch)

        # Re-run guardrails on the amended commit
        try:
            guardrails = MergeGuardrailsCheck(self.project_root)
            amended_sha = _run_git(
                self.project_root, "rev-parse", "HEAD",
                check=False, timeout=15,
            ).stdout.strip()
            gr_report = guardrails.check_merge_result(pre_sha, amended_sha)
        except Exception as exc:
            return RepairResult(
                success=False,
                error=f"Guardrails re-check failed after repair: {exc}",
            )

        if not gr_report.passed:
            # Still has violations after repair — report failure
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
        return "\n".join(lines) if lines else "(none)"

    def _format_spec_contents(self, contents: dict[str, str]) -> str:
        """Format spec contents dict for the repair prompt."""
        lines: list[str] = []
        for path, content in sorted(contents.items()):
            lines.append(f"--- {path} ---")
            lines.append(content)
            lines.append("")
        return "\n".join(lines) if lines else "(none)"

    def _is_spec_path(self, path: str) -> bool:
        """Return True when path matches se3/specs/**/spec.md."""
        normalized = path.replace("\\", "/")
        return bool(_SPEC_PATH_RE.match(normalized))

    def _call_llm(self, prompt: str) -> str:
        """Call LLM with the repair prompt."""
        from ..json_extractor import extract_json_two_phase
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
        parsed = extract_json_two_phase(
            raw,
            project_root=self.project_root,
            schema_hint=schema_hint,
            required_keys=["files"],
        )

        if parsed is not None:
            return json.dumps(parsed, ensure_ascii=False, indent=2)

        # Return raw for fallback parsing
        return raw

    def _parse_response(self, raw_response: str) -> Optional[dict[str, Any]]:
        """Parse LLM repair response as JSON dict."""
        from ..utils.json_parser import parse_json_response

        return parse_json_response(raw_response, required_keys=["files"])
