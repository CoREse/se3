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


def _has_any_conflict_marker(path: Path) -> bool:
    """Return True when the file at ``path`` still contains a conflict marker.

    Missing files (e.g. delete/modify conflicts where the LLM resolved
    the conflict by removing the file) are treated as "no markers" — a
    deleted file cannot harbour ``<<<<<<<``.  Read errors short-circuit
    to ``True`` so the caller treats the file as unresolved and re-tries
    rather than silently passing.
    """
    try:
        if not path.exists():
            return False
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning(
            "Failed to read %s when scanning for conflict markers: %s",
            path, exc,
        )
        return True
    return _has_conflict_markers(text)


def _scan_unresolved(paths: list[Path]) -> list[Path]:
    """Return the subset of ``paths`` whose files still contain conflict markers.

    The order of input paths is preserved in the output.
    """
    return [p for p in paths if _has_any_conflict_marker(p)]


_PREVIEW_CHARS = 500


def _preview(text: str) -> str:
    """Trim ``text`` to a short prefix suitable for storing on the outcome."""
    if not text:
        return ""
    if len(text) <= _PREVIEW_CHARS:
        return text
    return text[:_PREVIEW_CHARS] + f"… [+{len(text) - _PREVIEW_CHARS} chars]"


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
    """Conflict resolution strategy.

    Three tiers (the previous ``default`` / ``robust`` names have been
    removed without silent aliasing — see :meth:`from_str` for the
    migration error message):

    - ``fast`` (the new default): LLM resolves conflicts; on failure the
      merge exits without ever invoking a human, and never falls back to
      take-theirs.  Inherits the original robust strategy's
      dirty-worktree stash behavior.
    - ``safe``: LLM resolves conflicts; if the LLM cannot converge it
      falls back to a human MCP call.  Sets the same expectation of
      clean working tree as the legacy ``default`` strategy.
    - ``strict``: every conflict goes straight to a human call without
      invoking the LLM at all.
    """

    FAST = "fast"
    SAFE = "safe"
    STRICT = "strict"

    @classmethod
    def from_str(cls, value: str) -> "MergeStrategy":
        """Resolve a string to a :class:`MergeStrategy`, failing fast on
        the removed ``default`` / ``robust`` names.

        Unlike Python's default ``MergeStrategy(value)`` constructor,
        this method produces a migration-friendly error message pointing
        users at the replacement strategy.  Unknown values raise a
        ``ValueError`` listing the allowed names.
        """
        if not isinstance(value, str):
            raise ValueError(
                f"Merge strategy must be a string, got {type(value).__name__}={value!r}"
            )
        norm = value.strip().lower()
        if norm == "default":
            raise ValueError(
                "Merge strategy 'default' has been removed; use 'safe' instead "
                "(LLM-resolves conflicts, falls back to human MCP call on failure)."
            )
        if norm == "robust":
            raise ValueError(
                "Merge strategy 'robust' has been removed; use 'fast' instead "
                "(LLM-resolves conflicts, never falls back to take-theirs or human)."
            )
        try:
            return cls(norm)
        except ValueError as exc:
            allowed = ", ".join(m.value for m in cls)
            raise ValueError(
                f"Unknown merge strategy {value!r}; must be one of: {allowed}"
            ) from exc


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


@dataclass
class BatchContext:
    """Merge-level metadata for a single :meth:`ConflictResolver.resolve_batch` call.

    The fields mirror :class:`ConflictContext` but the per-file
    ``files`` list is decoupled — ``resolve_batch`` receives conflict
    files as a separate argument so the same context object can be
    re-used across iterations that operate on shrinking unresolved
    subsets.
    """

    project_root: Path
    ours_branch: str
    theirs_branch: str
    merge_base: str = ""
    ours_head_sha: str = ""
    ours_head_message: str = ""
    theirs_head_sha: str = ""
    theirs_head_message: str = ""
    ours_log_oneline: list[str] = field(default_factory=list)
    theirs_log_oneline: list[str] = field(default_factory=list)
    has_spec_files: bool = False
    strategy: MergeStrategy = MergeStrategy.FAST


@dataclass
class IterationFailure:
    """One iteration's failure state, fed back into the next iteration's prompt.

    Captures any of the five failure modes that the legacy JSON-decision
    pipeline used to escalate to take-theirs:

    * ``context_build_failed`` — building the conflict-context bundle raised.
    * ``llm_exception`` — the LLM subprocess raised or timed out.
    * ``parse_failed`` — pre-LLM-as-editor schema parsing failed.
    * ``apply_failed`` — writing the LLM's output failed.
    * ``markers_remaining`` — files still contain ``<<<<<<<``/``=======``/``>>>>>>>``.
    """

    iteration: int
    kind: str
    detail: str
    files: list[str] = field(default_factory=list)


@dataclass
class BatchResolveOutcome:
    """Result of :meth:`ConflictResolver.resolve_batch`.

    ``escalation_reason`` is ``None`` on success, otherwise one of:

    * ``"fast_failed"`` — fast strategy reached ``max_iterations`` with
      files still containing conflict markers; the caller MUST fail the
      merge without any human escalation.
    * ``"safe_to_human"`` — safe strategy reached ``max_iterations``;
      the caller MUST fall back to a human MCP call.
    * ``"strict_to_human"`` — strict strategy never invoked the LLM and
      routes every conflict file straight to a human MCP call.
    """

    resolved: list[Path] = field(default_factory=list)
    unresolved: list[Path] = field(default_factory=list)
    iterations_used: int = 0
    escalation_reason: Optional[str] = None
    history: list[IterationFailure] = field(default_factory=list)
    duration_sec: float = 0.0
    prompts_preview: list[str] = field(default_factory=list)
    responses_preview: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """True when no files remain with conflict markers and no
        escalation was triggered."""
        return self.escalation_reason is None and not self.unresolved


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
        strategy: MergeStrategy = MergeStrategy.SAFE,
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

    # ------------------------------------------------------------------
    # LLM-as-editor batch resolution (new model — see G3 in design doc).
    # The legacy ``resolve`` / ``_parse_response`` / ``_apply_resolution``
    # path is retained for the current orchestrator wiring and will be
    # removed by G4 once orchestrator.py adopts ``resolve_batch``.
    # ------------------------------------------------------------------

    def resolve_batch(
        self,
        conflict_files: list[ConflictFile],
        context: "BatchContext",
        *,
        max_iterations: int,
    ) -> "BatchResolveOutcome":
        """Resolve every conflicting file by asking the LLM to edit them in place.

        Each iteration:

        1. Builds an editor-style prompt listing every *currently
           unresolved* file with its base/ours/theirs/working content
           and tells the LLM to use its file-editing tools to remove
           every ``<<<<<<<`` / ``=======`` / ``>>>>>>>`` marker.
        2. Sends the prompt to the LLM (single call, all files
           bundled).
        3. Scans the on-disk versions of the targeted files; any file
           that still contains a conflict marker is retained for the
           next iteration's prompt, accompanied by the failure history
           of the previous iteration.

        The five legacy failure paths that used to fall back to
        take-theirs (context-build exception, LLM exception, parse
        failure, write/apply failure, leftover markers) are now all
        recorded as :class:`IterationFailure` entries and fed back into
        the next iteration's prompt — *never* into a take-theirs
        commit.

        Args:
            conflict_files: All conflicting files (post-``git merge``).
                Each :class:`ConflictFile` carries its working-tree
                relative path (interpreted relative to
                ``context.project_root``) plus the base/ours/theirs
                content captured before the LLM was invoked.
            context: Merge-level metadata shared across all files.
            max_iterations: Hard upper bound on the number of LLM
                calls.  When exhausted with files still unresolved, the
                outcome's ``escalation_reason`` is set to
                ``"fast_failed"`` and the caller decides what to do
                next (the strategy layer maps this to fail-fast for
                ``fast`` and to a human MCP call for ``safe``).

        Returns:
            A :class:`BatchResolveOutcome` describing which files were
            cleared, which remain, how many iterations were spent, and
            (when applicable) the per-iteration failure history that
            led to escalation.
        """
        if max_iterations < 1:
            raise ValueError(
                f"max_iterations must be >= 1, got {max_iterations}"
            )

        all_paths = [context.project_root / cf.path for cf in conflict_files]
        path_to_file = {
            context.project_root / cf.path: cf for cf in conflict_files
        }

        # Strict strategy never invokes the LLM — every conflicting
        # file routes straight to a human MCP call.  We still flag
        # every file as unresolved so the caller can build the call
        # file from the same outcome surface that fast/safe use.
        if context.strategy == MergeStrategy.STRICT:
            unresolved_now = _scan_unresolved(all_paths)
            return BatchResolveOutcome(
                resolved=[p for p in all_paths if p not in unresolved_now],
                unresolved=unresolved_now,
                iterations_used=0,
                escalation_reason="strict_to_human",
                history=[],
                duration_sec=0.0,
            )

        outcome = BatchResolveOutcome()
        history: list[IterationFailure] = []
        unresolved = _scan_unresolved(all_paths)

        # Edge case: caller passed in files that already happen to have
        # no markers (e.g. a previous run already cleared them).
        # Return success without burning an LLM call.
        if not unresolved:
            outcome.resolved = list(all_paths)
            outcome.unresolved = []
            outcome.iterations_used = 0
            return outcome

        t0 = time.monotonic()
        for iteration in range(1, max_iterations + 1):
            outcome.iterations_used = iteration

            iter_files = [path_to_file[p] for p in unresolved]
            prompt = self._build_editor_prompt(
                iter_files, context, history, iteration, max_iterations,
            )
            outcome.prompts_preview.append(_preview(prompt))

            try:
                response = self._call_llm(prompt)
            except Exception as exc:
                outcome.responses_preview.append("")
                history.append(IterationFailure(
                    iteration=iteration,
                    kind="llm_exception",
                    detail=redact_text(f"{type(exc).__name__}: {exc}"),
                    files=[str(p) for p in unresolved],
                ))
                logger.warning(
                    "resolve_batch iteration %d/%d: LLM call failed: %s",
                    iteration, max_iterations, exc,
                )
                # Re-scan in case the LLM partially wrote before failing.
                unresolved = _scan_unresolved(unresolved)
                if not unresolved:
                    break
                continue

            outcome.responses_preview.append(_preview(response))

            # The LLM was instructed to use file-editing tools, so the
            # primary success signal is "files no longer contain
            # markers" — independent of any text it printed.
            try:
                new_unresolved = _scan_unresolved(unresolved)
            except Exception as exc:
                history.append(IterationFailure(
                    iteration=iteration,
                    kind="apply_failed",
                    detail=redact_text(f"scan after write failed: {exc}"),
                    files=[str(p) for p in unresolved],
                ))
                logger.warning(
                    "resolve_batch iteration %d/%d: post-edit scan failed: %s",
                    iteration, max_iterations, exc,
                )
                # Be conservative: assume all targeted files are still
                # unresolved so the next iteration retries them.
                new_unresolved = list(unresolved)

            if not new_unresolved:
                unresolved = []
                break

            history.append(IterationFailure(
                iteration=iteration,
                kind="markers_remaining",
                detail=(
                    f"{len(new_unresolved)}/{len(unresolved)} files still "
                    f"contain conflict markers after iteration {iteration}"
                ),
                files=[str(p) for p in new_unresolved],
            ))
            unresolved = new_unresolved

        outcome.duration_sec = time.monotonic() - t0
        outcome.history = history
        outcome.unresolved = unresolved
        outcome.resolved = [p for p in all_paths if p not in unresolved]

        if unresolved:
            # Strategy layer maps "fast_failed" → fail merge, and (on
            # the safe path) overrides this to "safe_to_human" before
            # acting.  The resolver itself does not know which surface
            # the caller wants.
            outcome.escalation_reason = "fast_failed"

        return outcome

    def _build_editor_prompt(
        self,
        conflict_files: list[ConflictFile],
        context: "BatchContext",
        history: list[IterationFailure],
        iteration: int,
        max_iterations: int,
    ) -> str:
        """Construct the LLM-as-editor prompt for one iteration.

        The prompt asks the LLM to use its file-editing tools directly
        — there is no JSON schema, no per-hunk confidence reporting.
        Success is judged on a single observable: whether the working
        tree files still contain conflict markers after the call
        returns.
        """
        lines: list[str] = []

        lines.append(
            "You are resolving an in-progress `git merge` by directly editing the "
            "working-tree files listed below."
        )
        lines.append("")
        lines.append(
            "## Goal"
        )
        lines.append(
            "Edit each file so that **no** `<<<<<<<`, `=======`, or `>>>>>>>` "
            "conflict marker remains on disk. Use your file-editing tools "
            "(Edit / Write) directly — do NOT return JSON or print resolved "
            "content into your reply. The on-disk state is what counts."
        )
        lines.append("")
        lines.append(f"Iteration {iteration} of {max_iterations}.")
        lines.append("")

        # Merge metadata
        lines.append("## Merge Metadata")
        lines.append(f"- Project root: {context.project_root}")
        lines.append(f"- Current branch (ours): {context.ours_branch}")
        lines.append(f"- Incoming branch (theirs): {context.theirs_branch}")
        lines.append(f"- Merge base: {context.merge_base}")
        lines.append(f"- Ours HEAD: {context.ours_head_sha}")
        lines.append(f"- Theirs HEAD: {context.theirs_head_sha}")
        lines.append(f"- Strategy: {context.strategy.value}")
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

        if context.has_spec_files:
            lines.append(
                "⚠️  SPEC FILES PRESENT: do NOT delete requirements, weaken "
                "SHALL→SHOULD or MUST→SHOULD, weaken quantifiers (all→some), "
                "or delete scenarios. Merge both sides' content faithfully."
            )
            lines.append("")

        if history:
            lines.append("## Previous Iteration Outcomes")
            lines.append(
                "The previous iteration(s) did NOT clear all conflict markers. "
                "Each entry below describes what went wrong; consider whether "
                "you need a different approach this time."
            )
            for h in history[-3:]:  # only the last 3 entries to keep prompt size bounded
                lines.append(
                    f"- iteration {h.iteration}: {h.kind} — {h.detail}"
                )
                if h.files:
                    lines.append(
                        f"  files affected: {', '.join(h.files[:10])}"
                        + (" …" if len(h.files) > 10 else "")
                    )
            lines.append("")

        # Per-file blocks
        lines.append("## Files to resolve")
        lines.append("")
        for cf in conflict_files:
            abs_path = context.project_root / cf.path
            lines.append(f"### `{cf.path}`")
            lines.append(f"Absolute path: `{abs_path}`")
            if cf.is_spec:
                lines.append("[SPEC FILE — spec-guardrail rules apply]")
            if cf.is_binary:
                lines.append(
                    "[BINARY FILE — cannot be auto-edited. Choose a side "
                    "deliberately via `git checkout --ours`/`--theirs` "
                    "or leave it unresolved for human review.]"
                )
                lines.append("")
                continue
            if cf.hunks:
                lines.append(f"Conflict hunks: {len(cf.hunks)}")
                for hunk in cf.hunks:
                    lines.append(f"  Lines {hunk.start_line}-{hunk.end_line}")
            lines.append("")
            lines.append("#### Base version (common ancestor)")
            if cf.base_exists:
                lines.append("```")
                lines.append(cf.base_content)
                lines.append("```")
            else:
                lines.append("[file did not exist in base]")
            lines.append("")
            lines.append("#### Ours version (current branch)")
            if cf.ours_exists:
                lines.append("```")
                lines.append(cf.ours_content)
                lines.append("```")
            else:
                lines.append("[file did not exist in ours]")
            lines.append("")
            lines.append("#### Theirs version (incoming branch)")
            if cf.theirs_exists:
                lines.append("```")
                lines.append(cf.theirs_content)
                lines.append("```")
            else:
                lines.append("[file did not exist in theirs]")
            lines.append("")
            lines.append("#### Working tree (current state with conflict markers)")
            lines.append("```")
            lines.append(cf.working_content)
            lines.append("```")
            lines.append("")

        lines.append("## Rules")
        lines.append(
            "1. Combine both sides' intent faithfully; do NOT silently "
            "discard either side just to make conflicts go away."
        )
        lines.append(
            "2. For version-number conflicts (e.g. `pyproject.toml`), pick "
            "the higher SemVer value rather than concatenating; if both "
            "sides bumped, take whichever bump-type is larger."
        )
        lines.append(
            "3. Do not output JSON. Do not paste full resolved file content "
            "into your reply. Use your file-editing tools to write the "
            "resolved version directly to disk."
        )
        lines.append(
            "4. When you are done, the file SHALL contain no `<<<<<<<`, "
            "`=======`, or `>>>>>>>` marker lines."
        )
        lines.append(
            "5. Do not stage, commit, or `git add` anything — the caller "
            "handles staging and committing once all markers are gone."
        )

        return "\n".join(lines)

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
        if strategy == MergeStrategy.SAFE:
            lines.append(
                "Safe mode: resolve carefully. For spec files, be extra cautious. "
                "For regular files, resolve based on semantic correctness. "
                "On unresolved cases the merge falls back to a human MCP call."
            )
        elif strategy == MergeStrategy.STRICT:
            lines.append(
                "Strict mode: only accept resolutions you are highly confident about. "
                "If uncertain about any hunk, flag it for human review."
            )
        elif strategy == MergeStrategy.FAST:
            lines.append(
                "Fast mode: resolve aggressively for regular files, but NEVER weaken spec requirements. "
                "Spec files still require the same caution. There is no human fallback — "
                "if you cannot resolve a conflict, the merge will exit with a failure."
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
