"""ConflictResolver — LLM-driven conflict resolution with structured JSON output.

Constructs a detailed prompt from ConflictContext, calls LLMCaller,
and parses the structured JSON response containing resolved content,
per-hunk confidence scores, and flags.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from ..json_extractor import extract_json_two_phase
from ...commands.merge.secret_redact import redact_text
from .conflict_context import ConflictContext, ConflictFile, ConflictHunk

if TYPE_CHECKING:
    from ..llm_caller import LLMCaller

logger = logging.getLogger(__name__)


# D2: cap resolved file content at 5 MiB by default.  An LLM that hands
# back a multi-gigabyte string is either malfunctioning or attempting an
# OOM; either way we refuse to apply it.
DEFAULT_MAX_RESOLVED_CONTENT_BYTES = 5 * 1024 * 1024

# D3: git accepts up to 7 leading whitespace characters (spaces or tabs)
# on conflict markers (used by embedded diff blocks inside doc files).
# Detect markers anywhere in the line that come after at most 7
# space-or-tab characters; widening from `[ ]{0,7}` to `[ \t]{0,7}` so
# a `\t<<<<<<<` line cannot evade detection.  ``\s`` would be too
# broad (matches CR/LF and would let a multi-line marker slip), so we
# stay character-explicit but also accept a small handful of Unicode
# whitespace characters that LLMs occasionally emit (NBSP and the
# zero-width set) — see ``_has_conflict_markers`` for the rationale.
_CONFLICT_START_RE = re.compile(r"^[ \t]{0,7}<<<<<<<", re.MULTILINE)
_CONFLICT_MID_RE = re.compile(r"^[ \t]{0,7}={7,}\s*$", re.MULTILINE)
_CONFLICT_END_RE = re.compile(r"^[ \t]{0,7}>>>>>>>", re.MULTILINE)

# Unicode whitespace characters that LLMs occasionally emit by accident
# (or that get auto-substituted by some terminals / clipboard managers).
# Stripping these BEFORE the regex check means a ``" <<<<<<<"`` or
# ``"​<<<<<<<"`` evasion attempt is detected — closing a documented
# intent gap (the original implementation only tolerated ASCII space
# and tab).
_UNICODE_WHITESPACE_PREFIX_CHARS = (
    " "  # NO-BREAK SPACE
    " "  # OGHAM SPACE MARK
    " "  # EN QUAD
    " "  # EM QUAD
    " "  # EN SPACE
    " "  # EM SPACE
    " "  # THREE-PER-EM SPACE
    " "  # FOUR-PER-EM SPACE
    " "  # SIX-PER-EM SPACE
    " "  # FIGURE SPACE
    " "  # PUNCTUATION SPACE
    " "  # THIN SPACE
    " "  # HAIR SPACE
    "​"  # ZERO WIDTH SPACE
    " "  # NARROW NO-BREAK SPACE
    " "  # MEDIUM MATHEMATICAL SPACE
    "　"  # IDEOGRAPHIC SPACE
    "﻿"  # ZERO WIDTH NO-BREAK SPACE / BOM
)


def _normalize_unicode_whitespace_for_marker_detection(text: str) -> str:
    """Replace each Unicode whitespace prefix character with an ASCII space.

    The conflict-marker regexes match ``[ \\t]{0,7}`` before the
    ``<<<<<<<`` / ``>>>>>>>`` / ``=======`` triggers.  An LLM that emits
    a NBSP-prefixed marker would otherwise slip through.  We do NOT
    rewrite the original buffer that gets written to disk — only this
    detection scan sees the normalised form, so legitimate Unicode
    whitespace inside resolved content is preserved.
    """
    if not any(ch in text for ch in _UNICODE_WHITESPACE_PREFIX_CHARS):
        return text
    table = {ord(ch): " " for ch in _UNICODE_WHITESPACE_PREFIX_CHARS}
    return text.translate(table)


def _has_conflict_markers(text: str) -> bool:
    """Return True when ``text`` still contains conflict markers.

    Recognises markers preceded by up to 7 spaces or tabs (git's
    tolerance); a previous strict ``"<<<<<<<" in text`` check missed
    indented markers and let unresolved content slip through.

    Defense-in-depth: also recognises a stray ``=======`` divider line
    (the conflict-mid marker). A partial LLM edit that removes the
    surrounding ``<<<<<<<`` / ``>>>>>>>`` pair but leaves the divider
    behind would otherwise sneak through the start/end-only check;
    catching the divider closes that gap.

    The ``=======`` regex requires the seven equals to be the entire
    line content (after any leading whitespace) so that legitimate
    documentation prose like ``"the ======= notation"`` does not
    produce a false positive.

    Unicode whitespace tolerance: a subset of Unicode whitespace
    characters (NBSP, zero-width, ideographic space, BOM, etc.) is
    normalised to ASCII space before regex matching so a
    ``"\\u00a0<<<<<<<"`` or ``"\\ufeff<<<<<<<"`` evasion artifact is
    still detected.  The original buffer is NOT mutated; only the
    detection scan sees the normalised form.
    """
    scan_text = _normalize_unicode_whitespace_for_marker_detection(text)
    return bool(
        _CONFLICT_START_RE.search(scan_text)
        or _CONFLICT_MID_RE.search(scan_text)
        or _CONFLICT_END_RE.search(scan_text)
    )


class HunkValidationError(ValueError):
    """Raised when a HunkResolution payload is malformed."""


class ResolvedContentTooLargeError(ValueError):
    """Raised when an LLM resolution exceeds the configured size cap."""


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
    ROBUST = "robust"


@dataclass
class HunkResolution:
    """Resolution result for a single conflict hunk.

    ``start_line`` and ``end_line`` are 1-based and validated in
    ``__post_init__``: both must be positive integers and
    ``end_line >= start_line``.  Floats / strings / negatives /
    overflowed values raise :class:`HunkValidationError` rather than
    being silently coerced.
    """

    start_line: int
    end_line: int
    confidence: Confidence = Confidence.LOW
    reasoning: str = ""

    # Maximum line number we accept without an explicit file_lines
    # bound.  Anything larger almost certainly came from a buggy LLM
    # response and would be useless downstream.
    _MAX_LINE_NUMBER = 10_000_000

    def __post_init__(self) -> None:
        self.start_line = self._validate_line("start_line", self.start_line)
        self.end_line = self._validate_line("end_line", self.end_line)
        if self.end_line < self.start_line:
            raise HunkValidationError(
                f"end_line ({self.end_line}) must be >= start_line "
                f"({self.start_line})"
            )

    @classmethod
    def _validate_line(cls, name: str, value: object) -> int:
        # Reject None and any non-int / non-bool numeric type.  bool
        # is a subclass of int in Python so we filter it explicitly.
        if value is None:
            raise HunkValidationError(f"{name} must be an int, got None")
        # Accept stringified integers from LLM JSON output (e.g. "5").
        # Restrict to ASCII digits — `str.isdigit()` is True for many
        # non-ASCII numerals (e.g. Arabic-Indic ٠) on which int() raises
        # ValueError, and we don't want a malformed LLM response to
        # propagate ValueError out of conflict resolution.
        if isinstance(value, str):
            stripped = value.strip()
            if stripped and all("0" <= ch <= "9" for ch in stripped):
                try:
                    value = int(stripped)
                except ValueError as exc:
                    raise HunkValidationError(
                        f"{name} could not be parsed as int: {value!r}"
                    ) from exc
        if isinstance(value, bool) or not isinstance(value, int):
            raise HunkValidationError(
                f"{name} must be an int, got {type(value).__name__}={value!r}"
            )
        if value < 1:
            raise HunkValidationError(
                f"{name} must be >= 1 (got {value})"
            )
        if value > cls._MAX_LINE_NUMBER:
            raise HunkValidationError(
                f"{name} exceeds maximum allowed line number "
                f"({cls._MAX_LINE_NUMBER}); got {value}"
            )
        return value


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

    def __init__(
        self,
        project_root: Path,
        *,
        llm_caller: Optional["LLMCaller"] = None,
        llm_trace: Optional[Any] = None,
        max_resolved_content_bytes: int = DEFAULT_MAX_RESOLVED_CONTENT_BYTES,
    ) -> None:
        """Construct a resolver.

        Args:
            project_root: Repository root for git/log paths.
            llm_caller: Optional pre-built :class:`LLMCaller` to share
                across the merge pipeline (D9).  When supplied, every
                conflict resolution call reuses the same caller so that
                its prompt cache, retry budget, and trace stream remain
                continuous with downstream guardrail repair calls.  When
                ``None``, a fresh caller scoped to ``"merge_conflict"`` is
                built lazily on first use.
            llm_trace: Optional :class:`LLMTrace` for per-call jsonl
                recording (K2).  When supplied, every LLM call is timed
                and written to the trace file.
            max_resolved_content_bytes: Hard upper bound on
                ``resolved_content`` for any single file.  Defaults to
                5 MiB; configurable to allow tests to assert the cap is
                enforced (D2).
        """
        self.project_root = project_root
        self._llm_caller = llm_caller
        self._llm_trace = llm_trace
        if max_resolved_content_bytes <= 0:
            raise ValueError(
                "max_resolved_content_bytes must be positive, "
                f"got {max_resolved_content_bytes}"
            )
        self.max_resolved_content_bytes = max_resolved_content_bytes

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
        """Call LLM with the given prompt.

        Reuses ``self._llm_caller`` when one was injected at construction
        time, so prompt cache and retry budget stay shared with
        :class:`GuardrailRepairer` and any other merge-pipeline caller
        (see D9).  Falls back to a freshly-built caller when none was
        supplied.

        K2: If an :class:`LLMTrace` was injected, the call is timed and
        recorded as a jsonl entry.
        """
        caller = self._llm_caller
        if caller is None:
            from ..llm_caller import LLMCaller

            caller = LLMCaller(
                project_root=self.project_root,
                step_type="merge_conflict",
                max_retries=2,
                retry_delay=1.0,
            )

        t0 = time.monotonic()
        result: str = ""
        outcome: str = "success"
        error: Optional[str] = None
        try:
            result = caller.call(prompt=prompt, require_json=False)
        except Exception as exc:
            outcome = "error"
            error = str(exc)
            raise
        finally:
            if self._llm_trace is not None:
                try:
                    self._llm_trace.record(
                        agent="conflict_resolver",
                        prompt=redact_text(prompt),
                        response=redact_text(result) if outcome == "success" else "",
                        duration_sec=time.monotonic() - t0,
                        outcome=outcome,
                        error=error,
                    )
                except Exception as trace_exc:
                    logger.warning(
                        "LLM trace record failed (non-fatal): %s",
                        trace_exc,
                    )
        return result

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
            if not isinstance(resolved_content, str):
                # Coerce non-string types (LLMs sometimes hand back lists
                # or numbers).  Raise instead of silently stringifying:
                # the merge would otherwise commit garbage.
                raise HunkValidationError(
                    f"resolved_content for {path!r} must be a string, "
                    f"got {type(resolved_content).__name__}"
                )

            # D2: enforce size cap before any further processing.
            content_bytes = resolved_content.encode("utf-8", errors="replace")
            if len(content_bytes) > self.max_resolved_content_bytes:
                raise ResolvedContentTooLargeError(
                    f"resolved_content for {path!r} is "
                    f"{len(content_bytes)} bytes, exceeds cap of "
                    f"{self.max_resolved_content_bytes} bytes"
                )

            # Determine if this is a spec file by cross-referencing with context
            is_spec = False
            decoding_lossy = False
            for cf in context.files:
                if cf.path == path:
                    is_spec = cf.is_spec
                    decoding_lossy = bool(getattr(cf, "decoding_lossy", False))
                    break

            # Validate: no conflict markers in resolved content (D3 —
            # whitespace-tolerant detection).
            force_review = False
            if _has_conflict_markers(resolved_content):
                logger.warning(
                    "Resolved content for %s still contains conflict markers",
                    path,
                )
                force_review = True
            # G3 fix (medium): when conflict_context flagged the file
            # as decoded with errors='replace' (binary-adjacent or
            # non-UTF-8 input), the decoded text may have been
            # corrupted before the LLM ever saw it. Auto-accepting a
            # high-confidence resolution would commit garbage. Force
            # human review so the operator inspects the file rather
            # than letting a confident LLM mask data loss.
            if decoding_lossy:
                logger.warning(
                    "Forcing human review for %s: source content was "
                    "decoded with errors='replace' (decoding_lossy=True). "
                    "Auto-acceptance would risk committing corrupted "
                    "bytes back to disk.",
                    path,
                )
                force_review = True

            # Parse hunks
            hunks_data = file_data.get("hunks", [])
            hunks: list[HunkResolution] = []
            if isinstance(hunks_data, list):
                resolved_line_count = (
                    resolved_content.count("\n") + 1 if resolved_content else 0
                )
                for hunk_data in hunks_data:
                    if not isinstance(hunk_data, dict):
                        continue
                    try:
                        hunk = HunkResolution(
                            start_line=hunk_data.get("start_line", 0),
                            end_line=hunk_data.get("end_line", 0),
                            confidence=self._parse_confidence(
                                hunk_data.get("confidence", "low")
                            ),
                            reasoning=str(hunk_data.get("reasoning", "")),
                        )
                    except HunkValidationError as exc:
                        # D1: surface the malformed hunk rather than
                        # silently coercing nonsense values into the
                        # resolution.  Tests assert that negatives /
                        # huge / string values raise; production keeps
                        # the resolution flagged for human review and
                        # skips this hunk.
                        logger.warning(
                            "Discarding malformed hunk in %s: %s",
                            path, exc,
                        )
                        force_review = True
                        continue
                    # When file_lines is known, validate the hunk's
                    # end_line is within the resolved file's tail.
                    # If end_line points strictly past the resolved
                    # line count, the LLM likely produced metadata
                    # referring to the marker-decorated working tree
                    # — but the resolved file is what gets committed
                    # and the human-call file's hunk-localised view
                    # would point at garbage line numbers, defeating
                    # the operator's ability to inspect a localised
                    # hunk.  Force ``requires_human_review`` so the
                    # auto-accept path is rejected and the human-call
                    # file is written with a proper warning.
                    out_of_range = bool(
                        resolved_line_count
                        and hunk.end_line > resolved_line_count
                    )
                    if out_of_range:
                        logger.warning(
                            "Hunk end_line %d exceeds resolved-file line "
                            "count %d for %s — forcing human review because "
                            "downstream metadata would be misleading",
                            hunk.end_line, resolved_line_count, path,
                        )
                        force_review = True
                    hunks.append(hunk)

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
        """Create a fallback low-confidence resolution on parse failure.

        Defense-in-depth: when ``cf.working_content`` still contains
        conflict markers (the LLM produced no usable response, so we
        fell back to the unresolved working tree), the per-file flag
        ``contains_conflict_markers`` is set in addition to
        ``requires_human_review``.  Strategy gates SHOULD treat this
        as an unrecoverable failure rather than silently committing the
        working tree with ``<<<<<<<``/``=======``/``>>>>>>>`` markers.
        """
        files: list[FileResolution] = []
        any_markers = False
        for cf in context.files:
            has_markers = _has_conflict_markers(cf.working_content)
            if has_markers:
                any_markers = True
                logger.warning(
                    "Fallback resolution for %s contains unresolved conflict "
                    "markers — flagging for human review (parse_error=%s)",
                    cf.path, parse_error,
                )
            # Defense-in-depth: a malformed ``git diff --cc`` parse may
            # produce a ``ConflictHunk`` with non-positive line numbers.
            # Building a ``HunkResolution`` from those would raise
            # :class:`HunkValidationError` and escape the outer
            # ``_parse_response`` ``except``, crashing the resolver.
            # We swap such hunks for a placeholder ``(1, 1)`` so the
            # fallback path can always produce a valid LLMResolution
            # routed to human review.
            safe_hunks: list[HunkResolution] = []
            for h in cf.hunks:
                try:
                    safe_hunks.append(
                        HunkResolution(
                            start_line=h.start_line,
                            end_line=h.end_line,
                            confidence=Confidence.LOW,
                            reasoning="Parse failure — human review required",
                        )
                    )
                except HunkValidationError as h_exc:
                    logger.warning(
                        "Fallback resolution: hunk in %s has invalid line "
                        "numbers (start=%r end=%r): %s — substituting "
                        "placeholder hunk (1,1).",
                        cf.path, h.start_line, h.end_line, h_exc,
                    )
                    safe_hunks.append(
                        HunkResolution(
                            start_line=1,
                            end_line=1,
                            confidence=Confidence.LOW,
                            reasoning=(
                                "Parse failure — human review required "
                                "(original hunk had invalid line numbers)"
                            ),
                        )
                    )
            files.append(
                FileResolution(
                    path=cf.path,
                    resolved_content=cf.working_content,
                    hunks=safe_hunks,
                    overall_confidence=Confidence.LOW,
                    flags={
                        "requires_human_review": True,
                        "spec_guardrail_concern": cf.is_spec,
                        "contains_conflict_markers": has_markers,
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
                "contains_conflict_markers": any_markers,
            },
            parse_error=parse_error,
        )
