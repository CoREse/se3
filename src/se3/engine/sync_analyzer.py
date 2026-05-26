"""SE3 Sync Analyzer — LLM-driven spec-code comparison analysis.

Encapsulates the LLM-driven logic for comparing spec content against
project code and generating structured diff results.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .llm_caller import LLMCaller, LLMCallError
from .sync_engine import DiffType, SpecAnalysis, SpecDiff, strip_markdown_fences

# Sentinels used in SpecAnalysis.failed_analysis_reason. The analyzer
# distinguishes two failure modes so downstream reporting can suggest the
# right remediation:
#   * "infrastructure_failure" — LLM call exhausted retries, returned an
#     empty/near-empty response, or raised in transit. The agent never
#     produced output the framework could meaningfully evaluate.
#   * "llm_output_format_error" — agent did return content but it was not
#     valid JSON. The model is reachable; the prompt or parsing contract
#     is the suspect.
_REASON_INFRASTRUCTURE = "infrastructure_failure"
_REASON_OUTPUT_FORMAT = "llm_output_format_error"
_EMPTY_RESPONSE_THRESHOLD = 10

logger = logging.getLogger(__name__)

_ANALYSIS_JSON_SCHEMA = """\
{
  "diffs": [
    {
      "type": "gap | extension | conflict",
      "description": "Clear description of the difference",
      "spec_requirement": "The relevant spec requirement text",
      "code_location": "file_path:line_number or description of code area",
      "confidence": "high | low  (only for conflict type — how confident you are about resolving this automatically)"
    }
  ],
  "code_fully_absent": false
}"""

_CODE_ABSENCE_PROMPT = """\
## Code Absence Confirmation

All previously-known source files for this spec no longer exist on disk.
The subsystem described by this spec may have been entirely removed from the codebase.

Before reporting normal diffs, you MUST first determine whether ANY code described
by this spec still exists ANYWHERE in the project. Search broadly using Grep and
Glob for the subsystem's key identifiers (class names, function names, file paths
mentioned in the spec).

In your JSON response, set **code_fully_absent** to:

- **true** — After thorough search, you confirm that 100% of the code this spec
  describes has been removed. Every feature, function, and class the spec mentions
  no longer exists anywhere in the codebase.
- **false** — Some code related to this spec still exists (even if moved, renamed,
  or partially removed), OR you found at least one surviving file.

This field is REQUIRED. If you are uncertain, default to false (safer to keep the
spec than to wrongly delete it).\n\n"""

_ANALYSIS_PROMPT_TEMPLATE = """\
You are an expert software engineer performing a spec-code synchronization analysis.

## Task

Compare the specification below with the project's actual code and identify all differences.

## Spec: {spec_name}

{spec_content}

## Project Context

{project_context}

## Difference Types

Classify each difference into exactly one of these three types:

1. **gap** — The spec describes a requirement or feature that is NOT implemented in the code.
   - The spec says the system SHALL do X, but the code does not do X.
   - A scenario described in the spec has no corresponding implementation.

2. **extension** — The code contains functionality that the spec does NOT describe, and it does NOT contradict the spec.
   - A helper function, utility, or feature exists in code but is not mentioned in the spec.
   - Additional error handling or edge cases implemented beyond what the spec requires.

3. **conflict** — The code implements something DIFFERENTLY from what the spec describes.
   - The spec says "SHALL use JWT tokens" but the code uses session tokens.
   - The spec defines a specific API interface but the code uses a different signature.
   - Behavioral differences where the code contradicts the spec's stated requirements.
   - For each conflict, include a "confidence" field:
     - "high" — You are confident about which side (spec or code) is correct and how to resolve it.
     - "low" — The resolution is ambiguous and requires human judgment.

## Code Browsing Instructions

You MUST actively read and examine the project's source code to perform accurate analysis.
Do NOT rely on assumptions or the project context alone — verify every finding against real code.

- Use the **Read** tool to read relevant source files and check implementations.
- Use the **Grep** tool to search for specific functions, classes, or patterns mentioned in the spec.
- Use the **Glob** tool to discover files related to the spec's domain.

For each spec requirement, locate the corresponding code and verify whether it is implemented correctly.
Base your gap/extension/conflict analysis on what the code actually does, not on what you assume it does.

## Rules

- Be thorough: check every requirement and scenario in the spec.
- Be precise: cite specific spec requirements and code locations.
- If the spec and code are fully aligned, return an empty diffs array.
- Focus on functional differences, not stylistic or naming conventions.
- Do NOT report differences in comments, documentation style, or whitespace.

## Important: One-Directional Sync

This analysis drives a one-directional spec update — the spec will be
rewritten to match the code, never the reverse. Only report what the code
actually does today; ignore aspirations that exist in the spec but not in
the code (those will be removed). Do not flag missing-but-planned features
as gaps — gaps here mean "the spec describes something the code no longer
does." User intent for not-yet-built features belongs in the issue tracker,
not in this analysis.

## Output Format

Return a JSON object with this structure:

{json_schema}
"""

_BASE_SPEC_PROMPT_TEMPLATE = """\
You are an expert software engineer. Explore the project codebase and generate a base specification document.

## Task

Analyze the project structure, source code, configuration, and any existing documentation to produce a comprehensive base specification that captures the project's current state.

## Project Context

{project_context}

## Output Format

Write a Markdown specification document following this structure:

```markdown
# <Project Name> — Base Specification

## Purpose
<One paragraph describing what this project does and its primary goal.>

## Requirements

### Requirement: Project Identity
- Project name: <name>
- Description: <brief description>
- Primary language/framework: <language and frameworks used>

### Requirement: Directory Structure
<Describe the key directories and their purposes>

### Requirement: Coding Conventions
<Key coding patterns and conventions observed in the codebase>

### Requirement: Key Constraints
<Important constraints, limitations, or architectural decisions>
```

Be factual — describe what the code actually does, not what it should do.
Include specific file paths and patterns you observe.
"""


class SyncAnalyzer:
    """LLM-driven spec-code comparison analyzer.

    Builds structured prompts for LLM to compare spec content against project
    code and parses the results into SpecAnalysis data models.
    """

    def __init__(self, project_root: Path, llm_caller: LLMCaller) -> None:
        self.project_root = Path(project_root)
        self.llm_caller = llm_caller

    def _build_analysis_prompt(
        self,
        spec_name: str,
        spec_content: str,
        project_context: str,
        deps: Optional[List[str]] = None,
        all_deps_missing: bool = False,
    ) -> str:
        """Build the LLM prompt for spec-code comparison.

        Args:
            spec_name: Name of the spec being analyzed.
            spec_content: Full text content of the spec file.
            project_context: Project context string (file tree, git info, etc).
            deps: Optional list of dependency file paths (relative to project root).
                When non-empty and all_deps_missing is True, injects the code
                absence confirmation section into the prompt.
            all_deps_missing: Whether all files in deps no longer exist on disk.

        Returns:
            Formatted prompt string for the LLM.
        """
        base_prompt = _ANALYSIS_PROMPT_TEMPLATE.format(
            spec_name=spec_name,
            spec_content=spec_content,
            project_context=project_context,
            json_schema=_ANALYSIS_JSON_SCHEMA,
        )
        if deps and all_deps_missing:
            base_prompt += _CODE_ABSENCE_PROMPT
        return base_prompt

    def analyze_spec(
        self,
        spec_name: str,
        spec_content: str,
        project_context: str,
        deps: Optional[List[str]] = None,
    ) -> SpecAnalysis:
        """Analyze a single spec against project code using LLM.

        Calls LLMCaller with EXTRACT JSON mode, parses the structured output
        into a SpecAnalysis. Retries up to 3 times on LLM call failure.

        Args:
            spec_name: Name of the spec being analyzed.
            spec_content: Full text content of the spec.
            project_context: Project context string.
            deps: Optional list of dependency file paths (relative to project
                root). When non-empty and all files are missing from disk, the
                prompt includes a code absence confirmation section.

        Returns:
            SpecAnalysis with the list of diffs found.
        """
        all_deps_missing = self._check_all_deps_missing(deps)
        prompt = self._build_analysis_prompt(
            spec_name, spec_content, project_context,
            deps=deps, all_deps_missing=all_deps_missing,
        )

        max_attempts = 3
        last_error: Optional[Exception] = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = self.llm_caller.call(
                    prompt=prompt,
                    json_mode="extract",
                    json_schema_hint=_ANALYSIS_JSON_SCHEMA,
                    required_keys=["diffs"],
                )
                analysis = self._parse_analysis_response(spec_name, response)
                analysis.touched_files = sorted(self.llm_caller.last_touched_files)
                return analysis
            except (LLMCallError, Exception) as e:
                last_error = e
                logger.warning(
                    "SyncAnalyzer: attempt %d/%d failed for spec '%s': %s",
                    attempt, max_attempts, spec_name, e,
                )
                if attempt < max_attempts:
                    continue

        logger.error(
            "SyncAnalyzer: all %d attempts failed for spec '%s': %s",
            max_attempts, spec_name, last_error,
        )
        return SpecAnalysis(
            spec_name=spec_name,
            diffs=[],
            failed_analysis_reason=_REASON_INFRASTRUCTURE,
        )

    def _parse_analysis_response(self, spec_name: str, response: str) -> SpecAnalysis:
        """Parse LLM JSON response into a SpecAnalysis.

        Args:
            spec_name: Name of the spec being analyzed.
            response: JSON string from LLM.

        Returns:
            SpecAnalysis populated with parsed diffs.
        """
        # Strip markdown code fences before parsing — the LLM may wrap the
        # JSON in ```json ... ``` fences even in EXTRACT mode.
        stripped = strip_markdown_fences(response)
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as e:
            # Empty / near-empty / None responses are an infrastructure
            # signal (the agent never produced output). Non-empty but
            # malformed JSON is a contract violation by the LLM that we
            # bucket separately so reporting can hint at the right
            # remediation (retry vs. fix the prompt). Either way we do
            # NOT fabricate a CONFLICT diff: that historically poisoned
            # convergence by pretending the spec had real drift.
            if response is None or len(stripped.strip()) < _EMPTY_RESPONSE_THRESHOLD:
                reason = _REASON_INFRASTRUCTURE
            else:
                reason = _REASON_OUTPUT_FORMAT
            logger.error(
                "SyncAnalyzer: failed to parse JSON response for spec '%s' (%s): %s",
                spec_name, reason, e,
            )
            return SpecAnalysis(
                spec_name=spec_name,
                diffs=[],
                failed_analysis_reason=reason,
            )

        raw_diffs = data.get("diffs", [])
        diffs: List[SpecDiff] = []

        for item in raw_diffs:
            diff_type_str = item.get("type", "")
            try:
                diff_type = DiffType(diff_type_str)
            except ValueError:
                logger.warning(
                    "Unknown diff type '%s' in spec '%s', skipping", diff_type_str, spec_name
                )
                continue

            confidence = ""
            if diff_type == DiffType.CONFLICT:
                confidence = item.get("confidence", "low").lower()

            diffs.append(
                SpecDiff(
                    diff_type=diff_type,
                    spec_name=spec_name,
                    description=item.get("description", ""),
                    code_location=item.get("code_location", ""),
                    confidence=confidence,
                )
            )

        code_fully_absent = bool(data.get("code_fully_absent", False))

        return SpecAnalysis(
            spec_name=spec_name, diffs=diffs, code_fully_absent=code_fully_absent,
        )

    def _check_all_deps_missing(self, deps: Optional[List[str]]) -> bool:
        """Return True if deps is non-empty and ALL referenced files are absent."""
        if not deps:
            return False
        for rel_path in deps:
            abs_path = self.project_root / rel_path
            if abs_path.exists():
                return False
        return True

    def generate_base_spec(self, project_context: str) -> str:
        """Generate a base spec for projects that don't have one.

        Uses LLM to explore the project codebase and produce a base spec
        document. Writes the result to se3/specs/base/spec.md.

        Args:
            project_context: Project context string.

        Returns:
            The generated base spec content.

        Raises:
            LLMCallError: If LLM call fails after retries.
        """
        prompt = _BASE_SPEC_PROMPT_TEMPLATE.format(project_context=project_context)

        response = self.llm_caller.call(
            prompt=prompt,
            json_mode="off",
        )

        content = response.strip()
        content = strip_markdown_fences(content)

        base_dir = self.project_root / "se3" / "specs" / "base"
        base_dir.mkdir(parents=True, exist_ok=True)
        spec_path = base_dir / "spec.md"
        spec_path.write_text(content, encoding="utf-8")

        logger.info("Generated base spec at %s", spec_path)
        return content
