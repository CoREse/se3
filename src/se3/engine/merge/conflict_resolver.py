"""ConflictResolver — LLM-driven conflict resolution with structured JSON output.

Constructs a detailed prompt from ConflictContext, calls LLMCaller,
and parses the structured JSON response containing resolved content,
per-hunk confidence scores, and flags.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from ..json_extractor import extract_json_two_phase
from .conflict_context import ConflictContext, ConflictFile, ConflictHunk

logger = logging.getLogger(__name__)


class Confidence(str, Enum):
    """Confidence level for a resolution decision."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class MergeStrategy(str, Enum):
    """Conflict resolution strategy."""

    DEFAULT = "default"
    STRICT = "strict"
    FAST = "fast"


@dataclass
class HunkResolution:
    """Resolution result for a single conflict hunk."""

    start_line: int
    end_line: int
    confidence: Confidence = Confidence.LOW
    reasoning: str = ""


@dataclass
class FileResolution:
    """Resolution result for a single file."""

    path: str
    resolved_content: str = ""
    hunks: list[HunkResolution] = field(default_factory=list)
    overall_confidence: Confidence = Confidence.LOW
    flags: dict[str, bool] = field(default_factory=dict)
    is_spec: bool = False


@dataclass
class LLMResolution:
    """Complete LLM resolution result for a merge conflict."""

    files: list[FileResolution] = field(default_factory=list)
    overall_confidence: Confidence = Confidence.LOW
    flags: dict[str, bool] = field(default_factory=dict)
    raw_response: str = ""
    parse_error: Optional[str] = None


# JSON schema description for the LLM output
_RESOLUTION_SCHEMA = """{
  "files": [
    {
      "path": "relative/path/to/file",
      "resolved_content": "full resolved file content with no conflict markers",
      "hunks": [
        {
          "start_line": 1,
          "end_line": 10,
          "confidence": "high|medium|low",
          "reasoning": "brief explanation of the resolution choice"
        }
      ],
      "overall_confidence": "high|medium|low",
      "flags": {
        "requires_human_review": false,
        "spec_guardrail_concern": false
      }
    }
  ],
  "overall_confidence": "high|medium|low",
  "flags": {
    "requires_human_review": false,
    "spec_guardrail_concern": false
  }
}"""


class ConflictResolver:
    """Resolve merge conflicts using LLM with structured JSON output."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def resolve(
        self,
        context: ConflictContext,
        strategy: MergeStrategy = MergeStrategy.DEFAULT,
    ) -> LLMResolution:
        """Resolve conflicts in the given context.

        Constructs a detailed prompt from the context, calls the LLM,
        and parses the structured JSON response.

        Args:
            context: The three-way merge context.
            strategy: The conflict resolution strategy.

        Returns:
            An LLMResolution with resolved content and confidence scores.
            On parse failure, returns a low-confidence result with
            requires_human_review=True.
        """
        prompt = self._build_prompt(context, strategy)

        # Pass the raw prompt text to LLMCaller; the framework's own
        # _resolve_args() handles auto-filing when the prompt exceeds
        # the 100KB threshold. This preserves chat-history integrity
        # (the original text is recorded, not an @file reference) and
        # avoids redundant temp-file lifecycle management.
        raw_response = self._call_llm(prompt)

        if not raw_response:
            logger.warning("LLM returned empty response for conflict resolution")
            return self._fallback_resolution(context, "Empty LLM response")

        return self._parse_response(raw_response, context)

    def _call_llm(self, prompt: str) -> str:
        """Call LLM with the given prompt."""
        from ..llm_caller import LLMCaller

        caller = LLMCaller(
            project_root=self.project_root,
            step_type="merge_conflict",
            max_retries=2,
            retry_delay=1.0,
        )
        return caller.call(prompt=prompt, require_json=False)

    def _build_prompt(
        self,
        context: ConflictContext,
        strategy: MergeStrategy,
    ) -> str:
        """Build the conflict resolution prompt."""
        lines: list[str] = []

        lines.append("You are a git merge conflict resolver. Your task is to resolve ALL conflicts in the files below and output a structured JSON response.")
        lines.append("")
        lines.append("## Merge Metadata")
        lines.append(f"- Current branch (ours): {context.ours_branch}")
        lines.append(f"- Incoming branch (theirs): {context.theirs_branch}")
        lines.append(f"- Merge base: {context.merge_base}")
        lines.append(f"- Ours HEAD: {context.ours_head_sha}")
        lines.append(f"- Theirs HEAD: {context.theirs_head_sha}")
        lines.append("")

        if context.ours_head_message:
            lines.append(f"### Ours commit message\n{context.ours_head_message}")
            lines.append("")
        if context.theirs_head_message:
            lines.append(f"### Theirs commit message\n{context.theirs_head_message}")
            lines.append("")

        if context.ours_log_oneline:
            lines.append("### Commits on ours since merge base")
            for line in context.ours_log_oneline:
                lines.append(f"  {line}")
            lines.append("")

        if context.theirs_log_oneline:
            lines.append("### Commits on theirs since merge base")
            for line in context.theirs_log_oneline:
                lines.append(f"  {line}")
            lines.append("")

        # Strategy indicator
        lines.append(f"## Strategy: {strategy.value}")
        if strategy == MergeStrategy.DEFAULT:
            lines.append(
                "Default mode: resolve carefully. For spec files, be extra cautious. "
                "For regular files, resolve based on semantic correctness."
            )
        elif strategy == MergeStrategy.STRICT:
            lines.append(
                "Strict mode: only accept resolutions you are highly confident about. "
                "If uncertain about any hunk, flag it for human review."
            )
        elif strategy == MergeStrategy.FAST:
            lines.append(
                "Fast mode: resolve aggressively for regular files, but NEVER weaken spec requirements. "
                "Spec files still require the same caution as default mode."
            )
        lines.append("")

        # Spec file warning
        if context.has_spec_files:
            lines.append(
                "⚠️  SPEC FILES DETECTED: This merge involves spec files (se3/specs/**/spec.md). "
                "You MUST NOT delete requirements, weaken language (SHALL→SHOULD, MUST→SHOULD), "
                "weaken quantifiers (all→some), or delete scenarios. If any such change is needed, "
                "flag spec_guardrail_concern=true."
            )
            lines.append("")

        # Per-file sections
        lines.append("## Conflicting Files")
        lines.append("")

        for cf in context.files:
            lines.append(f"--- File: {cf.path} ---")
            if cf.is_binary:
                lines.append("[BINARY FILE — cannot resolve automatically]")
                lines.append("")
                continue

            if cf.is_spec:
                lines.append("[SPEC FILE — guardrails apply]")

            # Show hunk info
            if cf.hunks:
                lines.append(f"Conflict hunks: {len(cf.hunks)}")
                for hunk in cf.hunks:
                    lines.append(f"  Lines {hunk.start_line}-{hunk.end_line}")
            lines.append("")

            # Base version
            lines.append("### Base version (common ancestor)")
            if cf.base_exists:
                lines.append("```")
                lines.append(cf.base_content)
                lines.append("```")
            else:
                lines.append("[file did not exist in base]")
            lines.append("")

            # Ours version
            lines.append("### Ours version (current branch)")
            if cf.ours_exists:
                lines.append("```")
                lines.append(cf.ours_content)
                lines.append("```")
            else:
                lines.append("[file did not exist in ours]")
            lines.append("")

            # Theirs version
            lines.append("### Theirs version (incoming branch)")
            if cf.theirs_exists:
                lines.append("```")
                lines.append(cf.theirs_content)
                lines.append("```")
            else:
                lines.append("[file did not exist in theirs]")
            lines.append("")

            # Working tree (with conflict markers)
            lines.append("### Working tree (current state with conflict markers)")
            lines.append("```")
            lines.append(cf.working_content)
            lines.append("```")
            lines.append("")

        # Output instructions
        lines.append("## Output Format")
        lines.append("")
        lines.append(
            "Return ONLY a valid JSON object matching this schema (no markdown fences, no prose):"
        )
        lines.append("")
        lines.append(_RESOLUTION_SCHEMA)
        lines.append("")
        lines.append("Rules:")
        lines.append("1. resolved_content MUST be the COMPLETE file content with NO conflict markers.")
        lines.append("2. Each hunk must have confidence (high/medium/low) and a brief reasoning.")
        lines.append("3. overall_confidence should reflect your confidence across ALL files.")
        lines.append("4. flags.requires_human_review=true if ANY hunk is uncertain or a spec file has questionable changes.")
        lines.append("5. flags.spec_guardrail_concern=true if any spec file change might violate guardrails.")
        lines.append("6. For binary files, set resolved_content to empty string and requires_human_review=true.")

        return "\n".join(lines)

    def _parse_response(
        self,
        raw_response: str,
        context: ConflictContext,
    ) -> LLMResolution:
        """Parse the LLM's structured JSON response."""
        schema_hint = (
            "A JSON object with 'files' array, 'overall_confidence' string, "
            "and 'flags' dict. Each file has 'path', 'resolved_content', "
            "'hunks' array with 'start_line', 'end_line', 'confidence', 'reasoning', "
            "and 'overall_confidence' and 'flags'."
        )

        parsed = extract_json_two_phase(
            raw_response,
            project_root=self.project_root,
            schema_hint=schema_hint,
            required_keys=["files"],
        )

        if parsed is None:
            logger.warning("Failed to parse LLM conflict resolution JSON")
            return self._fallback_resolution(context, "JSON parse failure")

        try:
            return self._build_resolution_from_json(parsed, context, raw_response)
        except Exception as exc:
            logger.warning("Failed to build resolution from parsed JSON: %s", exc)
            return self._fallback_resolution(context, f"Resolution build error: {exc}")

    def _build_resolution_from_json(
        self,
        data: dict,
        context: ConflictContext,
        raw_response: str = "",
    ) -> LLMResolution:
        """Build LLMResolution from parsed JSON dict."""
        files_data = data.get("files", [])
        if not isinstance(files_data, list):
            files_data = []

        file_resolutions: list[FileResolution] = []
        global_flags = data.get("flags", {})
        if not isinstance(global_flags, dict):
            global_flags = {}

        for file_data in files_data:
            if not isinstance(file_data, dict):
                continue

            path = file_data.get("path", "")
            resolved_content = file_data.get("resolved_content", "")

            # Determine if this is a spec file by cross-referencing with context
            is_spec = False
            for cf in context.files:
                if cf.path == path:
                    is_spec = cf.is_spec
                    break

            # Validate: no conflict markers in resolved content
            force_review = False
            if "<<<<<<<" in resolved_content or ">>>>>>>" in resolved_content:
                logger.warning("Resolved content for %s still contains conflict markers", path)
                force_review = True

            # Parse hunks
            hunks_data = file_data.get("hunks", [])
            hunks: list[HunkResolution] = []
            if isinstance(hunks_data, list):
                for hunk_data in hunks_data:
                    if isinstance(hunk_data, dict):
                        hunks.append(
                            HunkResolution(
                                start_line=int(hunk_data.get("start_line", 0)),
                                end_line=int(hunk_data.get("end_line", 0)),
                                confidence=self._parse_confidence(
                                    hunk_data.get("confidence", "low")
                                ),
                                reasoning=str(hunk_data.get("reasoning", "")),
                            )
                        )

            file_flags = file_data.get("flags", {})
            if not isinstance(file_flags, dict):
                file_flags = {}
            if force_review:
                file_flags["requires_human_review"] = True

            file_resolutions.append(
                FileResolution(
                    path=path,
                    resolved_content=resolved_content,
                    hunks=hunks,
                    overall_confidence=self._parse_confidence(
                        file_data.get("overall_confidence", "low")
                    ),
                    flags={
                        "requires_human_review": bool(
                            file_flags.get("requires_human_review", False)
                        ),
                        "spec_guardrail_concern": bool(
                            file_flags.get("spec_guardrail_concern", False)
                        ),
                    },
                    is_spec=is_spec,
                )
            )

        # Build overall resolution
        overall_conf = self._parse_confidence(data.get("overall_confidence", "low"))

        return LLMResolution(
            files=file_resolutions,
            overall_confidence=overall_conf,
            flags={
                "requires_human_review": bool(
                    global_flags.get("requires_human_review", False)
                ),
                "spec_guardrail_concern": bool(
                    global_flags.get("spec_guardrail_concern", False)
                ),
            },
            raw_response=raw_response,
        )

    def _parse_confidence(self, value: str | None) -> Confidence:
        """Parse a confidence string to Confidence enum."""
        if not value:
            return Confidence.LOW
        try:
            return Confidence(value.lower())
        except ValueError:
            return Confidence.LOW

    def _fallback_resolution(
        self,
        context: ConflictContext,
        parse_error: str,
    ) -> LLMResolution:
        """Create a fallback low-confidence resolution on parse failure."""
        files: list[FileResolution] = []
        for cf in context.files:
            files.append(
                FileResolution(
                    path=cf.path,
                    resolved_content=cf.working_content,
                    hunks=[
                        HunkResolution(
                            start_line=h.start_line,
                            end_line=h.end_line,
                            confidence=Confidence.LOW,
                            reasoning="Parse failure — human review required",
                        )
                        for h in cf.hunks
                    ],
                    overall_confidence=Confidence.LOW,
                    flags={
                        "requires_human_review": True,
                        "spec_guardrail_concern": cf.is_spec,
                    },
                    is_spec=cf.is_spec,
                )
            )

        return LLMResolution(
            files=files,
            overall_confidence=Confidence.LOW,
            flags={
                "requires_human_review": True,
                "spec_guardrail_concern": context.has_spec_files,
            },
            parse_error=parse_error,
        )
