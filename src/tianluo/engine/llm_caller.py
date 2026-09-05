"""LLM caller for step execution.

Handles subprocess calls to Claude CLI with retry and fallback logic.
Manages agent selection and rotation on infrastructure errors.
"""

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from ..agent_runner import (
    AgentInvocationIntent,
    AgentRunner,
    InfraErrorType,
    RunnerStartupMetadata,
)
from ..claude_runner import ClaudeCodeRunner, ClaudeRunner
from ..stop_signal import get_stop_signal
from ..usage import (
    UsageEventAggregator,
    UsageRecord,
    expand_configured_model,
    parse_usage_record,
)
from .prompt_dedup import deduplicate_prompt_lines
from .retry_context import (
    POST_DEDUP_SAFETY_LIMIT,
    RETRY_HISTORY_MARKER,
    RETRY_HISTORY_SEPARATOR,
)
from .token_usage import UsageTotals, add_call_usage

logger = logging.getLogger(__name__)


def _post_dedup_safety_cap(
    effective_prompt: str, limit: int = POST_DEDUP_SAFETY_LIMIT
) -> str:
    """Defensive fallback: if dedup left the prompt above ``limit``, truncate
    the *head* of the retry-history section so the current new prompt (which
    lives at the tail after the separator) is preserved in full.

    Anchoring rules:
      * The retry-history marker MUST appear at position 0 (the current call
        site passes the marker-prefixed retry_context as the leading segment).
        A stray prefix would be silently discarded by the rebuild; assert to
        catch such a wiring change before it corrupts prompts.
      * The separator is located via ``rfind`` so that, in retry-of-retry
        chains where a prior ``effective_prompt`` (containing an inner
        marker+separator) is stored as a user message and replayed verbatim,
        the cap still resolves to the OUTER separator — the inner one
        cannot outrank it.

    No-op when ``effective_prompt`` is under ``limit`` or when no retry-history
    marker is found (first-call / non-retry path).
    """
    if len(effective_prompt) <= limit:
        return effective_prompt

    marker_idx = effective_prompt.find(RETRY_HISTORY_MARKER)
    if marker_idx < 0:
        # Not a retry context; refuse to truncate.  This shouldn't happen in
        # practice because only retry prompts exceed the limit, but stay safe.
        return effective_prompt

    # Current wiring always puts the marker at position 0 (retry_context is
    # the first segment of effective_prompt). Any prefix would be silently
    # discarded by the header+kept_body+tail rebuild below; fail loud if a
    # future caller prepends content before retry_context.
    assert marker_idx == 0, (
        f"_post_dedup_safety_cap expects retry_context at position 0, "
        f"got marker at {marker_idx} (prefix would be lost)"
    )

    # rfind: defense in depth against retry-of-retry chains where the stored
    # user content includes an inner separator. The outer separator is always
    # last in the emitted retry context (format_history_for_retry() appends
    # it immediately before the continuation notice).
    sep_idx = effective_prompt.rfind(RETRY_HISTORY_SEPARATOR)
    if sep_idx < marker_idx + len(RETRY_HISTORY_MARKER):
        # Marker present but no separator appears after it — structural
        # invariant violated (e.g. format_history_for_retry() returned early
        # or dedup collapsed the separator line). Cap cannot act without
        # knowing where the tail starts; log so the failure mode is
        # observable rather than silent.
        logger.warning(
            "Post-dedup safety cap could not act: effective_prompt %d chars > %d, "
            "retry-history marker found but separator missing; returning prompt unchanged.",
            len(effective_prompt),
            limit,
        )
        return effective_prompt

    # Include the separator INSIDE the tail so that the rebuilt output always
    # contains exactly one marker and exactly one separator (the structural
    # invariant the cap itself relies on for recursive anchoring on a next
    # retry). history_body is the content strictly between marker and
    # separator — it does not include either.
    tail = effective_prompt[sep_idx:]
    history_body = effective_prompt[marker_idx + len(RETRY_HISTORY_MARKER) : sep_idx]

    header = f"{RETRY_HISTORY_MARKER}\n[... retry history truncated (head) to stay under safety limit ...]\n"
    budget = limit - len(header) - len(tail)
    if budget <= 0:
        # Tail alone is too large — keep it intact (semantic priority) and
        # drop history_body entirely. Output still satisfies the invariant
        # because the separator lives inside tail. Dedicated warning
        # distinguishes this pathological branch from a normal cap trigger.
        truncated = header + tail
        logger.warning(
            "Post-dedup safety cap could not bound size (tail exceeds limit): "
            "effective_prompt %d chars > %d; tail alone is %d chars, kept intact. "
            "Output length %d chars still exceeds limit.",
            len(effective_prompt),
            limit,
            len(tail),
            len(truncated),
        )
        return truncated
    if budget < len(RETRY_HISTORY_SEPARATOR):
        # Budget is positive but too small to contain a whole history line;
        # skip kept_body entirely to avoid emitting a partial-line fragment.
        # The separator inside `tail` remains the only anchor. Output fits
        # under ``limit`` (budget > 0 ⇒ len(header) + len(tail) < limit).
        truncated = header + tail
        logger.warning(
            "Post-dedup safety cap triggered: effective_prompt %d chars > %d; "
            "truncated retry-history head to %d chars (kept_body skipped — budget too small).",
            len(effective_prompt),
            limit,
            len(truncated),
        )
        return truncated

    if budget < len(history_body):
        # Slice by character count, then round forward to the next newline so
        # the kept body always begins at a clean line boundary (avoids a half
        # `[User Prompt]:` header at the start).  If no newline exists in the
        # sliced region, fall back to the raw slice.
        raw_slice = history_body[-budget:]
        nl_idx = raw_slice.find("\n")
        kept_body = raw_slice[nl_idx + 1 :] if nl_idx >= 0 else raw_slice
    else:
        kept_body = history_body
    truncated = header + kept_body + tail

    logger.warning(
        "Post-dedup safety cap triggered: effective_prompt %d chars > %d; "
        "truncated retry-history head to %d chars.",
        len(effective_prompt),
        limit,
        len(truncated),
    )
    return truncated

# Module-level extra prompt state for Ctrl+C injection (transient, consumed once
# PER CALLER — see ``_extra_prompt_epoch``)
_extra_prompt: Optional[str] = None
# Persistent extra prompt state for loop context injection (survives across LLM calls)
_persistent_extra_prompt: Optional[str] = None
# Lock protecting _extra_prompt and _persistent_extra_prompt for thread safety
_extra_prompt_lock = threading.Lock()
# Bumped every time the transient slot is armed. WHY an epoch rather than
# clearing the slot on first read: a DAG IMPLEMENT step runs several groups in
# parallel, each with its own LLMCaller, and the dialog conclusion is addressed
# to the STEP — every group resumed as part of that execution must receive it.
# Clearing on first read handed it to whichever thread got there first and left
# the others resuming with no idea what the user decided. Each LLMCaller records
# the epoch it consumed, so the instruction reaches every caller exactly once and
# still never repeats on a second call by the same caller. The slot itself is
# closed by the step scope (``clear_transient_extra_prompt``).
_extra_prompt_epoch: int = 0


def set_extra_prompt(prompt: Optional[str], persistent: bool = False) -> None:
    """Set an extra prompt to inject into LLM calls.

    Args:
        prompt: The prompt text to inject, or None to clear.
        persistent: If True, the prompt survives across multiple LLM calls
                   (used for loop context injection). If False (default),
                   the prompt is consumed after one LLM call (used for
                   Ctrl+C interrupt injection).
    """
    with _extra_prompt_lock:
        if persistent:
            global _persistent_extra_prompt
            _persistent_extra_prompt = prompt
        else:
            global _extra_prompt, _extra_prompt_epoch
            _extra_prompt = prompt
            if prompt:
                _extra_prompt_epoch += 1


def get_extra_prompt() -> Optional[str]:
    """Get the current extra prompt (None if not set).

    Returns the combined transient + persistent prompt without consuming either.
    """
    with _extra_prompt_lock:
        parts = []
        if _persistent_extra_prompt:
            parts.append(_persistent_extra_prompt)
        if _extra_prompt:
            parts.append(_extra_prompt)
        return "\n\n".join(parts) if parts else None


def clear_extra_prompt() -> None:
    """Clear both transient and persistent extra prompts."""
    with _extra_prompt_lock:
        global _extra_prompt, _persistent_extra_prompt
        _extra_prompt = None
        _persistent_extra_prompt = None


def clear_transient_extra_prompt() -> None:
    """Clear only the transient extra prompt.

    This is what CLOSES a step-scoped injection — the LLM calls that read the
    slot no longer clear it (each caller only records the epoch it took, so a
    DAG step's parallel groups all receive the instruction). Without this close,
    the slot would stay armed into an unrelated later step; a step that makes no
    LLM call at all (TEST, COMMIT) never even reads it.
    """
    with _extra_prompt_lock:
        global _extra_prompt
        _extra_prompt = None


#: Agent ``type`` → runner class, for capability probes that must not pay the
#: cost (or the import) of building a runner instance.
_RUNNER_RESUME_SUPPORT_CACHE: Dict[str, bool] = {}


def runner_supports_native_resume(agent_type: Optional[str]) -> bool:
    """Whether the runner for *agent_type* declares native session resume.

    A pure capability probe for callers that need the answer BEFORE a call is
    made (the interjection dialog, deciding whether to promise a same-session
    conversation). The strategy decision itself stays inside LLMCaller — this
    only reads the runner class attribute the charter's layering makes the
    runner's to declare.
    """
    key = str(agent_type or "claude-code")
    cached = _RUNNER_RESUME_SUPPORT_CACHE.get(key)
    if cached is not None:
        return cached
    supported = False
    try:
        if key == "claude-code":
            supported = bool(ClaudeCodeRunner.supports_native_resume)
        elif key == "codex":
            from tianluo.codex_runner import CodexRunner

            supported = bool(CodexRunner.supports_native_resume)
        elif key == "claude-interactive":
            from tianluo.claude_interactive_runner import ClaudeInteractiveRunner

            supported = bool(ClaudeInteractiveRunner.supports_native_resume)
    except Exception:  # pragma: no cover - an unimportable runner cannot resume
        logger.debug("Failed to probe resume support for %r", key, exc_info=True)
        supported = False
    _RUNNER_RESUME_SUPPORT_CACHE[key] = supported
    return supported


def clear_persistent_extra_prompt() -> None:
    """Clear only the persistent extra prompt (for cleanup between loop iterations)."""
    with _extra_prompt_lock:
        global _persistent_extra_prompt
        _persistent_extra_prompt = None


def _same_directory(a: Any, b: Any) -> bool:
    """Whether two path-likes name the same directory, symlinks resolved."""
    try:
        return Path(str(a)).resolve() == Path(str(b)).resolve()
    except (OSError, ValueError):  # pragma: no cover - defensive
        return str(a) == str(b)


#: The framing a post-dialog continuation opens with, on either strategy.
#: WHY it is a constant rather than inline text: the native and the rebuilt
#: continuation must tell the agent the SAME story about why it stopped, or the
#: fallback silently changes what the user's decision meant.
_DIALOG_RESUME_FRAMING = (
    "[Continuation after a user interruption]\n"
    "The user interrupted your run to discuss it with you. That "
    "discussion has concluded. Before doing anything else, check "
    "the workspace for half-finished edits from your interrupted "
    "attempt and reconcile them. Then continue from where you "
    "stopped — do NOT redo work you already completed."
)

#: Headers of the framework injections appended AFTER a step template's own
#: output contract (charter, code-index, runtime environment). The contract
#: search stops at the first of these: a project charter containing a ```json
#: example would otherwise be quoted back to the agent as the required reply
#: shape.
_POST_CONTRACT_INJECTION_HEADERS = (
    "\n\n## Project Charter\n",
    "\n\n## Code Index (project structure map)\n",
    "\n## tianluo Runtime Environment\n",
    "\n## SE3 Runtime Environment\n",
)


# Set by the interjection-dialog "continue" decision immediately before the
# step is re-run. It only selects the FRAMING of the continuation directive —
# "you were interrupted by the user and the discussion has concluded" rather
# than "the previous attempt failed" — because the two situations call for
# different first moves from the agent (reconcile a half-finished workspace vs.
# retry a failure). The instruction text itself travels through the existing
# extra-prompt channel, so there is one carrier, not two.
_dialog_resume_pending = False
#: Epoch companion of ``_dialog_resume_pending``; see ``_extra_prompt_epoch``.
_dialog_resume_epoch: int = 0


def mark_dialog_resume() -> None:
    """Flag the next LLM call as a post-interjection-dialog continuation."""
    global _dialog_resume_pending, _dialog_resume_epoch
    with _extra_prompt_lock:
        _dialog_resume_pending = True
        _dialog_resume_epoch += 1


def consume_dialog_resume() -> bool:
    """Return and clear the post-dialog continuation flag.

    The step-scope closer (``run_step``'s finally) and tests use this. A single
    LLM call sequence does NOT: it takes the per-caller epoch view instead
    (``LLMCaller._take_dialog_resume``) so parallel DAG groups do not race for
    one flag.
    """
    global _dialog_resume_pending
    with _extra_prompt_lock:
        value = _dialog_resume_pending
        _dialog_resume_pending = False
        return value


def clear_phase1_cache(project_root: Path, flow_id: str, step_id: str) -> None:
    """Clear the Phase 1 cache file for a step.

    Called when a step is being restarted from scratch (revision or fix loop),
    so the next run performs a fresh Phase 1 LLM call instead of reusing a
    cached output from a previous attempt.

    Args:
        project_root: Project root directory
        flow_id: Flow instance ID
        step_id: Step instance ID
    """
    from .chat_history import _history_dir
    cache_path = _history_dir(project_root, flow_id) / f"{step_id}_phase1.txt"
    if cache_path.exists():
        try:
            cache_path.unlink()
            logger.info(f"Cleared Phase 1 cache for step {step_id}")
        except OSError as e:
            logger.warning(f"Failed to clear Phase 1 cache for {step_id}: {e}")


from ..i18n import t
from .tool_formatters import (
    build_tool_detail_payload,
    build_tool_in_flight_detail_payload,
    format_tool_chip_header,
    format_tool_chip_in_flight_header,
    format_tool_diff,
    format_tool_result_preview,
    format_tool_use_preview,
    set_project_root,
    truncate_preview,
)


class LLMCallError(Exception):
    """Error during LLM call."""

    pass


class StreamJSONTracker:
    """Tracks and prints real-time summary for stream-json output.

    Processes each line of NDJSON output immediately and prints a summary,
    allowing users to see progress as Claude Code runs.
    """

    # Maximum number of cached tool entries before oldest are evicted
    _MAX_CACHE_SIZE = 100

    # Coalesce streamed assistant text/thinking into batched progress lines:
    # a run of small text chunks is flushed as ONE stream_progress record once
    # it crosses this many characters (or when a non-text semantic event or the
    # stream end forces a flush). This keeps progress writes at semantic-event
    # granularity rather than one append per tiny chunk (the acceptance
    # criterion "非逐字节行").
    _PROGRESS_FLUSH_CHARS = 200

    def __init__(
        self,
        stream_prefix: str = '',
        project_root: Optional[Path] = None,
        flow_id: Optional[str] = None,
        step_id: Optional[str] = None,
        step_type: str = '',
        attempt: int = 0,
        usage_attempt: Optional[int] = None,
        agent_name: Optional[str] = None,
        runner_type: Optional[str] = None,
        provider: Optional[str] = None,
        provider_session_id: Optional[str] = None,
        reported_model: Optional[str] = None,
        configured_model: Optional[str] = None,
        runner_startup_model: Optional[str] = None,
        resolved_model: Optional[str] = None,
        call_id: Optional[str] = None,
        on_agent_change: Optional[Callable[[str, Optional[str]], None]] = None,
    ):
        self.stream_prefix = stream_prefix
        self.message_count = 0
        self.tool_calls = []
        self.tool_results = []
        self.text_chunks = 0
        self.total_text_len = 0
        self.start_time = time.time()
        self._last_ended_with_newline = True
        self._tool_use_id_to_name: Dict[str, str] = {}  # Map tool_use_id -> tool_name
        self._tool_use_id_to_input: Dict[str, dict] = {}  # Cache Edit/Write inputs for diff
        self._tool_use_id_to_old_content: Dict[str, Optional[str]] = {}  # Cache Write target file content
        self._touched_files: Set[str] = set()
        self._project_root = project_root
        # Flow context for incremental progress recording. Progress lines are
        # only written when BOTH flow_id and step_id are set (the same gate
        # LLMCaller uses for record_prompt / record_response); without them the
        # tracker behaves exactly as before (stdout rendering only).
        self._flow_id = flow_id
        self._step_id = step_id
        self._step_type = step_type or ''
        self._attempt = attempt
        # Identity of the agent behind this attempt (e.g. "dclaude"), fixed at
        # construction so every stream_progress record can label its
        # accumulating bubble with the agent the moment the first fragment
        # streams — before the final assistant result lands. On agent rotation
        # the caller builds a fresh tracker with the new agent's name, so each
        # attempt's progress lines carry their own real agent.
        self._agent_name = agent_name
        # Optional notification fired once the actual model name is parsed from
        # the stream's init/system metadata, as (agent_name, model_name). Used by
        # DAG-parallel implement to upgrade the group's live status card from
        # "agent" to "agent · model". The initial (agent, None) notification is
        # the caller's responsibility (fired at attempt selection, before the
        # tracker exists); the tracker only adds the model upgrade. Called
        # best-effort — a faulty callback must never disturb stream processing.
        self._on_agent_change = on_agent_change
        # Actual model name (e.g. "claude-opus-4-8"), best-effort parsed from
        # the stream's init/system metadata as lines arrive. Stays None until a
        # model is seen; once set, subsequent progress lines carry it so the
        # frontend upgrades the bubble label from "agent" to "agent · model".
        self._model_name: Optional[str] = None
        # Provider session id already durably recorded for this attempt. Seeded
        # with the caller's pre-spawn id (the Claude adapters pre-allocate one,
        # and the prompt record already names it) so only a session the STREAM
        # announces — codex mints its thread id itself — costs an extra record.
        self._recorded_session_id: Optional[str] = provider_session_id
        # Pending coalesced text/thinking awaiting a flush.
        self._progress_text_buf: List[str] = []
        self._progress_text_len = 0
        self._usage_aggregator = UsageEventAggregator(
            call_id=call_id,
            attempt=attempt if usage_attempt is None else usage_attempt,
            agent_name=agent_name,
            runner_type=runner_type,
            provider=provider,
            provider_session_id=provider_session_id,
            reported_model=reported_model,
            configured_model=configured_model,
            runner_startup_model=runner_startup_model,
            resolved_model=resolved_model,
        )

    @property
    def _progress_enabled(self) -> bool:
        return bool(self._flow_id and self._step_id)

    def _emit_progress(
        self,
        content: str,
        raw_obj: Any,
        *,
        tool_use_id: Optional[str] = None,
        is_error: Optional[bool] = None,
        tool_detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append one in-progress line to the step jsonl (best-effort).

        No-op unless flow context is set. A write failure is swallowed so the
        in-flight LLM stream is never disrupted by history I/O.

        Optional ``tool_use_id`` / ``is_error`` / ``tool_detail`` kwargs feed
        the frontend's single-chip state machine. Both chip states carry
        ``tool_use_id`` and a structured ``tool_detail``: the in-flight one a
        ``kind="tool_input"`` payload (the call's full arguments, expandable
        while it runs), the terminal one the settled
        ``build_tool_detail_payload`` result.

        INVARIANT: ``is_error`` — absent while in flight, ``True``/``False``
        once settled — is the ONLY marker distinguishing the two states, on
        both sides of the wire. ``tool_detail`` is populated in both, so no
        caller may read its absence as "still running".

        They are forwarded to ``record_stream_progress`` only when non-default
        so narrative-text progress lines stay byte-identical to the legacy
        schema.
        """
        if not self._progress_enabled or not content:
            return
        try:
            from .chat_history import record_stream_progress

            extra: Dict[str, Any] = {}
            if tool_use_id is not None:
                extra["tool_use_id"] = tool_use_id
            if is_error is not None:
                extra["is_error"] = is_error
            if tool_detail is not None:
                extra["tool_detail"] = tool_detail
            # Carry the agent identity on every progress line (when known) so
            # the bubble shows its agent from the first fragment; carry the
            # model only once it has been parsed from the stream metadata so
            # the bubble upgrades to "agent · model" in place.
            if self._agent_name is not None:
                extra["agent_name"] = self._agent_name
            if self._model_name is not None:
                extra["model_name"] = self._model_name

            record_stream_progress(
                self._project_root or Path.cwd(),
                self._flow_id,
                self._step_id,
                self._step_type,
                content,
                raw_obj,
                self._attempt,
                **extra,
            )
        except Exception:  # pragma: no cover - defensive; never break the stream
            logger.debug("Failed to record stream progress", exc_info=True)

    def _backfill_prompt_session_id(self, session_id: str) -> None:
        """Point this attempt's prompt record at the just-captured session id.

        Best-effort and never fatal: the stream-announced identity record is
        the durable binding on its own, so a failed rewrite degrades to the
        pre-existing behaviour rather than disturbing the live stream.
        """
        if not self._progress_enabled or not session_id:
            return
        try:
            from .chat_history import backfill_prompt_session_id

            backfill_prompt_session_id(
                self._project_root or Path.cwd(),
                self._flow_id,
                self._step_id,
                attempt=self._attempt,
                session_id=session_id,
                agent_name=self._agent_name,
            )
        except Exception:  # pragma: no cover - defensive; never break the stream
            logger.debug("Failed to backfill prompt session id", exc_info=True)

    def emit_agent_identity(self) -> None:
        """Emit an identity-only progress record at attempt start.

        The web console builds its accumulating assistant bubble from the FIRST
        ``stream_progress`` fragment of a turn. Without this seed, the bubble —
        and therefore the agent badge — only appears once the first text/tool
        fragment streams; a call that produces no intermediate fragments (or
        only returns a final result) would leave the current reply area without
        the real agent name for the whole call. This writes one record carrying
        the attempt's ``agent_name`` (and ``model_name`` if already known) so
        the agent shows the moment the attempt starts, before any model output.

        Unlike :meth:`_emit_progress`, the content is intentionally EMPTY: the
        frontend renders empty content as nothing, so only the agent badge
        appears until real fragments arrive. No-op without flow context or a
        known agent name; a write failure is swallowed so the in-flight LLM
        stream is never disrupted.
        """
        if not self._progress_enabled or self._agent_name is None:
            return
        try:
            from .chat_history import record_stream_progress

            extra: Dict[str, Any] = {"agent_name": self._agent_name}
            if self._model_name is not None:
                extra["model_name"] = self._model_name
            if self._recorded_session_id:
                extra["provider_session_id"] = self._recorded_session_id
            record_stream_progress(
                self._project_root or Path.cwd(),
                self._flow_id,
                self._step_id,
                self._step_type,
                "",  # identity-only seed: no visible content, just the badge
                None,
                self._attempt,
                **extra,
            )
        except Exception:  # pragma: no cover - defensive; never break the stream
            logger.debug("Failed to record agent identity progress", exc_info=True)

    def _buffer_progress_text(self, text: str) -> None:
        """Accumulate streamed text/thinking; flush when it crosses the batch
        threshold so writes stay at semantic-event granularity."""
        if not self._progress_enabled or not text:
            return
        self._progress_text_buf.append(text)
        self._progress_text_len += len(text)
        if self._progress_text_len >= self._PROGRESS_FLUSH_CHARS:
            self._flush_progress_text()

    def _flush_progress_text(self) -> None:
        """Emit any buffered text/thinking as a single coalesced progress line."""
        if not self._progress_text_buf:
            return
        content = "".join(self._progress_text_buf)
        self._progress_text_buf = []
        self._progress_text_len = 0
        self._emit_progress(content, None)

    def _handle_tool_result(self, tool_use_id: str, content: Any, is_error: bool) -> None:
        """Handle a single tool_result event.

        Shared by both the legacy top-level tool_result format and the
        newer type='user' nested format.

        Emits exactly one terminal ``stream_progress`` record for the chip
        keyed by ``tool_use_id`` — content is the merged
        ``format_tool_chip_header`` (success ``✓`` or failure ``✗``) wrapped
        in ``[...]`` so the frontend's ``TOOL_MARKER_RE`` matches both live
        and final state with the same grammar. The record carries the
        structured ``tool_detail`` payload (built from cached use input + old
        content) so the chip's collapsible detail panel can render an
        equivalent CLI-style view (Edit diff with line numbers, Read text,
        Bash stdout/stderr, etc.) without a second round-trip.

        CLI stdout (the ``✅`` / ``❌`` emoji lines) is unchanged.
        """
        self.tool_results.append(tool_use_id)
        tool_name = self._tool_use_id_to_name.get(tool_use_id, '')

        # Flush pending text first so the recorded progress order matches stdout.
        self._flush_progress_text()

        # Pop the per-id caches up front so the terminal-chip emit and the
        # CLI diff renderer see the same snapshot of use_input / old_content.
        cached_input = self._tool_use_id_to_input.pop(tool_use_id, None)
        old_content = self._tool_use_id_to_old_content.pop(tool_use_id, None)

        if is_error:
            error_preview = truncate_preview(str(content)) if content else "Unknown error"
            print(
                f"  {self.stream_prefix}[llm-stream] "
                + t("engine.llm.stream.tool_error", preview=error_preview)
            )
            if tool_name:
                header = format_tool_chip_header(
                    tool_name, cached_input, content, is_error=True
                )
                chip_content = f"[{header}]"
                detail = build_tool_detail_payload(
                    tool_name, cached_input, content, old_content=old_content
                )
            else:
                # Unknown tool (orphan tool_result) — fall back to the
                # frontend-friendly "[Tool error: ...]" marker; no per-tool
                # detail payload is meaningful here.
                chip_content = f"[Tool error: {error_preview}]"
                detail = None
            self._emit_progress(
                chip_content,
                None,
                tool_use_id=tool_use_id or None,
                is_error=True,
                tool_detail=detail,
            )
        else:
            preview = format_tool_result_preview(tool_name, content)
            print(f"  {self.stream_prefix}[llm-stream] ✅ {preview}...")
            if tool_name:
                header = format_tool_chip_header(
                    tool_name, cached_input, content, is_error=False
                )
                chip_content = f"[{header}]"
                detail = build_tool_detail_payload(
                    tool_name, cached_input, content, old_content=old_content
                )
            else:
                # Unknown tool — keep the legacy single-preview marker (no
                # detail builder available) so the frontend regex still
                # matches.
                chip_content = f"[{preview}]"
                detail = None
            self._emit_progress(
                chip_content,
                None,
                tool_use_id=tool_use_id or None,
                is_error=False,
                tool_detail=detail,
            )
            # Render diff for Edit/Write tools (CLI stdout only).
            if cached_input and tool_name in ("Edit", "Write"):
                format_tool_diff(tool_name, cached_input, content, old_content=old_content)

        self._tool_use_id_to_name.pop(tool_use_id, None)
        self._last_ended_with_newline = True

    def process_line(self, line: str) -> None:
        """Process a single line of NDJSON output."""
        line = line.strip()
        if not line:
            return

        # ANSI color codes
        GRAY = "\033[90m"      # Bright black (silver/gray)
        ITALIC = "\033[3m"     # Italic
        RESET = "\033[0m"      # Reset

        try:
            data = json.loads(line)
            msg_type = data.get('type', '')
            self._usage_aggregator.add_event(data)

            # A session id the stream announces (codex's ``thread.started``,
            # converted to an ``init`` line) is durably recorded the instant it
            # is known, not when the turn ends. The provider thread already
            # exists at this point; a parent killed before the response record
            # lands would otherwise leave it named nowhere, and every later
            # retry would rebuild from history instead of resuming it. The
            # write itself is deferred past the model-parse block below so an
            # init line carrying BOTH lands as one identity record.
            streamed_session = self._usage_aggregator.provider_session_id
            session_pending = bool(
                streamed_session and streamed_session != self._recorded_session_id
            )
            if session_pending:
                self._recorded_session_id = streamed_session
                # INVARIANT: both records of one attempt name the same session.
                # A capture-only provider mints its id itself, so the prompt
                # record was necessarily written pre-spawn with none; the id is
                # written back onto it here, inside the same attempt, rather
                # than leaving the pair inconsistent (or, worse, seeding the
                # prompt record with an id that was never this attempt's).
                self._backfill_prompt_session_id(streamed_session)

            # Best-effort parse the actual model name from this line's
            # init/system metadata. The first match is cached so subsequent
            # progress lines carry "agent · model"; a parse miss leaves the
            # cache untouched and never disturbs the stream.
            if self._model_name is None:
                from .chat_history import extract_model_name_from_obj

                model = extract_model_name_from_obj(data)
                if model:
                    self._model_name = model
                    # Immediately emit an identity-only progress record so the
                    # current reply bubble's badge upgrades from "agent" to
                    # "agent · model" the moment the model is known — without
                    # waiting for the next text/tool fragment (which may be a
                    # long pause away, or never come for a result-only call).
                    # The record carries empty content (badge-only) and now also
                    # the freshly parsed model_name via _emit_progress's extras.
                    self.emit_agent_identity()
                    # It carries the session id too, so a binding announced on
                    # this same line is already durable.
                    session_pending = False
                    # Notify the consumer that the actual model is now known so
                    # it can upgrade its label to "agent · model". Best-effort;
                    # a callback error must never disturb the stream.
                    if self._on_agent_change is not None and self._agent_name is not None:
                        try:
                            self._on_agent_change(self._agent_name, self._model_name)
                        except Exception:  # pragma: no cover - defensive
                            logger.debug(
                                "on_agent_change(agent, model) notify failed",
                                exc_info=True,
                            )

            if session_pending:
                self.emit_agent_identity()

            if msg_type == 'assistant':
                self.message_count += 1
                message = data.get('message', {})
                content = message.get('content', [])
                for item in content:
                    if isinstance(item, dict):
                        item_type = item.get('type', '')
                        if item_type == 'text':
                            text = item.get('text', '')
                            if text:
                                self.text_chunks += 1
                                self.total_text_len += len(text)
                                # Stream full text content directly
                                print(text, end='', flush=True)
                                self._last_ended_with_newline = text.endswith('\n')
                                # Buffer for incremental progress (coalesced).
                                self._buffer_progress_text(text)
                        elif item_type == 'thinking':
                            thinking = item.get('thinking', '')
                            if thinking:
                                # Stream thinking content in gray italic
                                print(f"{GRAY}{ITALIC}{thinking}{RESET}", end='', flush=True)
                                self._last_ended_with_newline = thinking.endswith('\n')
                                self._buffer_progress_text(thinking)
                        elif item_type == 'tool_use':
                            name = item.get('name', 'unknown')
                            tool_input = item.get('input', {})
                            tool_use_id = item.get('id', '')
                            self.tool_calls.append(name)
                            if tool_use_id:
                                self._tool_use_id_to_name[tool_use_id] = name
                                # Cache every tool's input so the terminal-chip
                                # emit on tool_result can build a merged header
                                # (input summary + result summary) and a
                                # structured detail payload via
                                # build_tool_detail_payload. The Write-tool
                                # old-content snapshot remains specific to
                                # diff rendering.
                                self._tool_use_id_to_input[tool_use_id] = tool_input
                                if name == "Write":
                                    file_path = tool_input.get("file_path", "")
                                    if file_path:
                                        try:
                                            self._tool_use_id_to_old_content[tool_use_id] = Path(file_path).read_text(encoding="utf-8")
                                        except (OSError, UnicodeDecodeError):
                                            self._tool_use_id_to_old_content[tool_use_id] = None
                                    else:
                                        self._tool_use_id_to_old_content[tool_use_id] = None
                                # Evict oldest entries if cache exceeds limit
                                if len(self._tool_use_id_to_input) > self._MAX_CACHE_SIZE:
                                    oldest = next(iter(self._tool_use_id_to_input))
                                    self._tool_use_id_to_input.pop(oldest, None)
                                    self._tool_use_id_to_old_content.pop(oldest, None)
                                    self._tool_use_id_to_name.pop(oldest, None)
                            # Record touched file paths for dependency tracking
                            if name == 'Read':
                                fp = tool_input.get('file_path', '')
                                if fp:
                                    self._record_touched_path(fp)
                            elif name in ('Grep', 'Glob'):
                                fp = tool_input.get('path', '')
                                if fp:
                                    self._record_touched_path(fp)
                            # Format and print tool_use preview
                            preview = format_tool_use_preview(name, tool_input)
                            # Only add leading newline if previous output didn't end with one
                            if not self._last_ended_with_newline:
                                print()
                            print(f"  {self.stream_prefix}[llm-stream] 🔧 {preview}...")
                            self._last_ended_with_newline = True
                            # Flush any pending text first so progress lines keep
                            # the same order the user sees on stdout, then record
                            # the tool_use as its own semantic progress line.
                            self._flush_progress_text()
                            in_flight = format_tool_chip_in_flight_header(name, tool_input)
                            self._emit_progress(
                                f"[{in_flight}]",
                                item,
                                tool_use_id=tool_use_id or None,
                                # INVARIANT: the omitted `is_error` — NOT an
                                # empty tool_detail — is what marks this record
                                # in-flight. The payload carries the call's full
                                # input so a running tool chip can be expanded
                                # instead of showing only its truncated header;
                                # the terminal emit on tool_result replaces it.
                                tool_detail=build_tool_in_flight_detail_payload(
                                    name, tool_input
                                ),
                            )

            elif msg_type == 'tool_result':
                # Legacy top-level tool_result format (backward compat)
                result = data.get('result', {})
                tool_use_id = result.get('toolUseId', result.get('tool_use_id', 'unknown'))
                content = result.get('content', '')
                is_error = result.get('isError', result.get('is_error', False))
                self._handle_tool_result(tool_use_id, content, is_error)

            elif msg_type == 'user':
                # CLI actual format: tool_result blocks nested inside user messages
                message = data.get('message', {})
                msg_content = message.get('content', [])
                for item in msg_content:
                    if isinstance(item, dict) and item.get('type') == 'tool_result':
                        tool_use_id = item.get('tool_use_id', 'unknown')
                        content = item.get('content', '')
                        is_error = item.get('is_error', False)
                        self._handle_tool_result(tool_use_id, content, is_error)

            elif msg_type == 'error':
                error_msg = data.get('error', 'Unknown error')
                print(
                    f"  {self.stream_prefix}[llm-stream] "
                    + t(
                        "engine.llm.stream.error",
                        preview=truncate_preview(str(error_msg)),
                    )
                )
                self._flush_progress_text()
                self._emit_progress(f"[Tool error: {truncate_preview(str(error_msg))}]", None)
                self._last_ended_with_newline = True

            elif msg_type == 'result':
                pass

        except json.JSONDecodeError:
            # Not valid JSON, might be a partial line
            pass

    def _capture_usage(self, data: Dict[str, Any]) -> None:
        """Compatibility entry point for callers feeding parsed result events."""
        try:
            self._usage_aggregator.add_event(data)
        except Exception:  # pragma: no cover - defensive; never break the stream
            logger.debug("Failed to capture usage from result message", exc_info=True)

    @property
    def usage(self) -> UsageTotals:
        """Legacy projection of all usage events seen in this attempt."""
        return UsageTotals.from_usage_record(self.usage_record)

    @property
    def usage_record(self) -> UsageRecord:
        """Authoritative usage record, including unavailable attempts."""
        return self._usage_aggregator.to_record()

    @property
    def session_id(self) -> Optional[str]:
        """Session identity this attempt actually ran under, if known yet.

        The stream-announced id wins over the pre-spawn seed: a capture-only
        provider mints its own thread id, so the seed is empty and only the
        stream can name the live session.
        """
        return self._usage_aggregator.provider_session_id or self._recorded_session_id

    def _record_touched_path(self, path: str) -> None:
        """Record a file path touched by a tool, normalized to project-relative."""
        p = Path(path)
        if self._project_root and p.is_absolute():
            try:
                p = p.relative_to(self._project_root)
            except ValueError:
                return
        self._touched_files.add(str(p))

    @property
    def touched_files(self) -> Set[str]:
        """Return the set of project-relative file paths touched by tool calls."""
        return set(self._touched_files)

    def print_summary(self) -> None:
        """Print final summary of the stream."""
        # Flush any trailing buffered text as a last progress line before the
        # stream's final result is recorded by LLMCaller._record_response.
        self._flush_progress_text()
        duration = time.time() - self.start_time
        print(
            f"  {self.stream_prefix}[llm-stream] "
            + t(
                "engine.llm.stream.complete",
                messages=self.message_count,
                tool_calls=len(self.tool_calls),
                chars=self.total_text_len,
                duration=f"{duration:.1f}",
            )
        )
        # Clean up caches to prevent memory leaks on stream interruption
        self._tool_use_id_to_input.clear()
        self._tool_use_id_to_old_content.clear()
        self._tool_use_id_to_name.clear()


class SessionCaptureRelay:
    """Session-identity sidecar wrapped around a caller-supplied ``on_output``.

    WHY it exists: the default streaming path builds a :class:`StreamJSONTracker`,
    which durably names a capture-only provider's session (codex mints its
    ``thread.started`` id itself, so nothing can be written pre-spawn) the
    instant the stream announces it. A caller that supplies its own
    ``on_output`` bypasses that tracker entirely — and without this relay the
    attempt's prompt record would keep its empty pre-spawn id forever while the
    response record, which falls back to the id parsed out of the stream, names
    the real one. That splits ONE attempt across two identities, violating the
    invariant that both of an attempt's records name the same session; worse, a
    parent that dies before the response lands leaves the live provider session
    named nowhere and therefore unresumable.

    Detection reuses :class:`UsageEventAggregator` rather than re-implementing a
    key scan, so the relay and the tracker can never disagree about what counts
    as a session announcement.
    """

    def __init__(
        self,
        delegate: Optional[Callable[[str], None]],
        *,
        project_root: Optional[Path] = None,
        flow_id: Optional[str] = None,
        step_id: Optional[str] = None,
        attempt: int = 0,
        agent_name: Optional[str] = None,
        seed_session_id: Optional[str] = None,
    ) -> None:
        self._delegate = delegate
        self._project_root = project_root
        self._flow_id = flow_id
        self._step_id = step_id
        self._attempt = attempt
        self._agent_name = agent_name
        self._aggregator = UsageEventAggregator(provider_session_id=seed_session_id)
        # Already durably recorded for this attempt; the pre-allocating adapters
        # seed it, so only an id the STREAM announces costs a backfill.
        self._recorded_session_id: Optional[str] = seed_session_id or None

    @property
    def session_id(self) -> Optional[str]:
        """Session identity observed for this attempt, or the seed."""
        return self._aggregator.provider_session_id or self._recorded_session_id

    def __call__(self, line: str) -> None:
        self._capture(line)
        if self._delegate is not None:
            self._delegate(line)

    def _capture(self, line: str) -> None:
        if not line or not line.strip():
            return
        try:
            data = json.loads(line.strip())
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        try:
            self._aggregator.add_event(data)
        except Exception:  # pragma: no cover - defensive; never break the stream
            logger.debug("Session capture failed to consume stream line", exc_info=True)
            return
        streamed = self._aggregator.provider_session_id
        if not streamed or streamed == self._recorded_session_id:
            return
        self._recorded_session_id = streamed
        self._backfill(streamed)

    def _backfill(self, session_id: str) -> None:
        """Point this attempt's prompt record at the just-captured session id.

        Best-effort and never fatal: a failed rewrite degrades to the previous
        behaviour rather than disturbing the live stream.
        """
        if not (self._flow_id and self._step_id):
            return
        try:
            from .chat_history import backfill_prompt_session_id

            backfill_prompt_session_id(
                self._project_root or Path.cwd(),
                self._flow_id,
                self._step_id,
                attempt=self._attempt,
                session_id=session_id,
                agent_name=self._agent_name,
            )
        except Exception:  # pragma: no cover - defensive; never break the stream
            logger.debug("Failed to backfill prompt session id", exc_info=True)


class LLMCaller:
    """Manages LLM calls within flow engine steps.

    Wraps agent runners with flow-engine-specific retry, JSON handling,
    chat history, and agent rotation logic.  Maintains a list of available
    agents and rotates to the next one on infrastructure errors (usage
    limit, timeout, hang).  Task-level failures are *not* rotated — those
    are left for the State Machine layer to retry.
    """

    def __init__(
        self,
        project_root: Optional[Path] = None,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        flow_id: Optional[str] = None,
        step_id: Optional[str] = None,
        step_type: Optional[str] = None,
        external_attempt: int = 0,
        retry_mode: str = "continue",
        agents: Optional[List[Dict[str, Any]]] = None,
        stream_prefix: str = '',
        fix_iteration: int = 0,
        self_check_pass_index: Optional[int] = None,
        on_agent_change: Optional[Callable[[str, Optional[str]], None]] = None,
        force_read_only: bool = False,
        resume_strategy: Optional[str] = None,
        generation: Optional[int] = None,
        resume_binding: Optional[Dict[str, Any]] = None,
        resume_fallback_prompt: Optional[str] = None,
        deny_shell: bool = False,
    ):
        self.project_root = Path(project_root) if project_root else Path.cwd()
        # Explicit "continue THIS provider session" instruction, used by the
        # interjection dialog to talk to the working agent inside its own
        # session. Unlike the history-driven resume plan it needs no flow/step
        # context — which is the point: the dialog deliberately records nothing
        # to the step jsonl through LLMCaller, so its machine-facing prompt
        # never becomes part of a later rebuilt retry context. Its prompt is
        # sent VERBATIM (no continuation framing): it is a question, not a
        # resumption of the task.
        self._resume_binding = dict(resume_binding) if resume_binding else None
        # The prompt to send when an EXPLICIT resume binding cannot be honoured
        # (the agent is gone, its runner has no resume, or the provider refused
        # the session). Such a fallback call is answered by an interlocutor
        # with none of that session's memory, so the caller supplies a
        # self-contained prompt — for the dialog, one carrying the rebuilt step
        # conversation — rather than the session-relative one.
        self._resume_fallback_prompt = resume_fallback_prompt
        # Continuation strategy for this caller. ``native`` (the default)
        # resumes the recorded provider session in place whenever one is
        # reachable; ``rebuild`` always reconstructs context from the step
        # jsonl. Resolved once here so a single call sequence cannot straddle
        # two strategies mid-flight.
        if resume_strategy is None:
            try:
                from ..config import load_resume_strategy

                resume_strategy = load_resume_strategy(self.project_root)
            except Exception:  # pragma: no cover - config faults must not abort
                logger.debug("Failed to load resume_strategy", exc_info=True)
                resume_strategy = "native"
        self.resume_strategy = resume_strategy
        # Rewind generation this call belongs to. Records are stamped with it
        # and the retry-context rebuild filters on it, so a step re-entered
        # after a rewind never resurrects the generation it was rewound away
        # from. Resolved per STEP (a rewind re-assigns only the steps it
        # removes; a pre-target step re-entered by the fix loop keeps its own
        # generation), read from the ambient flow state so the dozens of
        # LLMCaller construction sites stay untouched.
        if generation is None:
            from .rewind import current_generation

            generation = current_generation(flow_id=flow_id, step_id=step_id)
        self.generation = int(generation or 0)
        # 0 means "no flow generation published" (an ad-hoc caller outside a
        # run_step scope); records stamped 0 read as the legacy wildcard, which
        # is the right degradation for a call that belongs to no generation.
        # Instruction text injected into the *rebuilt* prompt by
        # ``set_extra_prompt``. Captured in ``call()`` so a native resume — which
        # sends no rebuilt prompt at all — can still carry it as its new user
        # turn; without this the user's dialog instruction would be silently
        # dropped exactly on the path the dialog exists to drive.
        self._pending_injected_instruction: Optional[str] = None
        # Epochs of the process-wide dialog slots this caller has already taken.
        # 0 means "nothing taken yet"; the slots' epochs start at 1.
        self._taken_extra_prompt_epoch = 0
        self._taken_dialog_resume_epoch = 0
        # Set when an attempt's native resume was rejected and the sequence fell
        # back to a rebuilt call. Read by the interjection dialog, which must
        # stop claiming (and recording) a same-session conversation the provider
        # has refused.
        self.native_resume_rejected = False
        # WHY: decouples the step's registry-level read_only from a single LLM
        # call's read-only posture. charter_freshness declares read_only=False
        # (its handler writes tianluo/charter.md), yet its LLM sub-calls must stay
        # read-only — they only PROPOSE candidate charter text, the handler's
        # Python does the writing. Passing force_read_only=True re-applies both
        # the prompt READ-ONLY injection and the runner --disallowedTools lock
        # for that call without touching is_step_read_only (the shared source of
        # truth). Default False preserves current behavior for every other step.
        self.force_read_only = force_read_only
        # Strict read-only posture: the runner must close the shell (and
        # subagent delegation) as well as the edit tools. WHY it is not implied
        # by read-only: the Claude CLIs run with --dangerously-skip-permissions,
        # so a read-only step keeps Bash on purpose (git diff, grep) — but the
        # interruption dialog talks to an agent whose workspace the user is
        # about to decide the fate of, and there "read-only" has to mean the
        # tree cannot be touched at all.
        self.deny_shell = deny_shell
        # Optional notification invoked whenever the agent selected for an
        # attempt is known — once as (agent_name, None) when the attempt starts
        # (including each rotation, so retries surface their real agent) and
        # again as (agent_name, model_name) once the stream reveals the actual
        # model. Used by DAG-parallel implement to keep a group's live
        # "running in worktree" status card labelled with its current agent
        # (then "agent · model"). Always invoked defensively via
        # ``_notify_agent_change`` so a faulty callback can never break a call.
        self._on_agent_change = on_agent_change
        # 1-based self_check pass index used to select the per-pass agent
        # chain when ``llm_caller.steps.self_check`` is a nested list. Only
        # meaningful for the self_check step; ignored otherwise.
        self.self_check_pass_index = self_check_pass_index
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.flow_id = flow_id
        self.step_id = step_id
        self.step_type = step_type or ""
        self.external_attempt = external_attempt  # Track external retry (e.g., from implement.py)
        self.retry_mode = retry_mode  # 'continue' (resume from breakpoint) or 'retry' (restart)
        self.stream_prefix = stream_prefix
        # Tag chat_history records with this iteration and filter retry-context
        # to messages from the same iteration, so reusing one step_id across
        # fix-loop iterations (implement step) does not leak prior iterations'
        # full conversations into the next call's prompt.
        self.fix_iteration = fix_iteration

        # Last raw result text from `type: "result"` NDJSON message.
        # Available after call() returns, for callers that need the full
        # LLM output text (not just the parsed JSON).
        self.last_raw_result: Optional[str] = None

        # Last set of project-relative file paths touched by Read/Grep/Glob
        # tool calls during the most recent call(). Used by SyncAnalyzer to
        # build per-spec dependency sets.
        self._last_touched_files: Set[str] = set()

        # Agent management
        # Resolution order when ``agents`` is not explicitly provided:
        #   1. Per-step override from ``llm_caller.steps.<step_type>`` — if
        #      declared, it is a HARD override with no fallback to the
        #      default chain. Exhausting it fails the call; users who want
        #      a default-claude tail must list it explicitly in the step's
        #      override.
        #   2. Otherwise, the default chain from ``load_agents`` (top-level
        #      ``agents`` / legacy ``claude_commands`` / built-in default).
        if agents is not None:
            self._agents = agents
        else:
            from ..config import resolve_agents
            resolved, is_override = resolve_agents(
                self.project_root, self.step_type,
                self_check_pass_index=self.self_check_pass_index,
            )
            if is_override:
                logger.info(
                    "Using per-step agent override for step '%s' (%d agent(s))",
                    self.step_type, len(resolved),
                )
            self._agents = resolved
        if not self._agents:
            raise ValueError(
                "LLMCaller requires a non-empty agents list; got empty. "
                "Check llm_caller.defaults / llm_caller.steps override "
                "or the explicit agents argument."
            )
        self._current_agent_index = 0
        self._runner_cache: Dict[str, AgentRunner] = {}

        # Legacy: expose a single _runner for backward compat (uses current agent)
        self._runner = self._get_current_runner()

    def _create_runner(self, agent_config: Dict[str, Any]) -> AgentRunner:
        """Create a Runner instance for the given agent config.

        Args:
            agent_config: Agent dict with name, type, cmd, priority.

        Returns:
            An AgentRunner implementation.
        """
        agent_type = agent_config.get("type", "claude-code")
        if agent_type == "claude-code":
            return ClaudeCodeRunner(
                project_root=self.project_root,
                command={"cmd": agent_config["cmd"], "priority": agent_config.get("priority", 0)},
            )
        if agent_type == "codex":
            from tianluo.codex_runner import CodexRunner
            return CodexRunner(
                project_root=self.project_root,
                command={
                    "cmd": agent_config["cmd"],
                    "priority": agent_config.get("priority", 0),
                    "provider": agent_config.get("provider"),
                },
            )
        if agent_type == "claude-interactive":
            # PTY-driven interactive Claude Code runner.  Lazily imported so the
            # core CLI never requires pexpect unless this opt-in type is used.
            from tianluo.claude_interactive_runner import ClaudeInteractiveRunner
            return ClaudeInteractiveRunner(
                project_root=self.project_root,
                command={"cmd": agent_config["cmd"], "priority": agent_config.get("priority", 0)},
            )
        # Future: add other agent types here
        raise ValueError(f"Unknown agent type: {agent_type}")

    def _get_current_runner(self) -> AgentRunner:
        """Get (or create and cache) the Runner for the current agent."""
        agent = self._agents[self._current_agent_index]
        cache_key = agent.get("name", agent.get("cmd", str(self._current_agent_index)))
        if cache_key not in self._runner_cache:
            self._runner_cache[cache_key] = self._create_runner(agent)
        return self._runner_cache[cache_key]

    @property
    def last_touched_files(self) -> Set[str]:
        """Return the set of project-relative file paths touched by the most
        recent ``call()`` invocation's Read/Grep/Glob tool calls."""
        return set(self._last_touched_files)

    def _notify_agent_change(
        self, agent_name: str, model_name: Optional[str]
    ) -> None:
        """Fire the optional ``on_agent_change`` notification, swallowing any
        error so a faulty consumer callback never breaks an in-flight call.

        Invoked with ``model_name=None`` when an attempt's agent is first
        selected (and on every rotation, so each attempt reports its own real
        agent) and again with the parsed ``model_name`` once the stream's
        init/system metadata reveals it.
        """
        if self._on_agent_change is None:
            return
        try:
            self._on_agent_change(agent_name, model_name)
        except Exception:  # pragma: no cover - defensive; never break the call
            logger.debug("on_agent_change notification failed", exc_info=True)

    def _rotate_agent(self) -> bool:
        """Advance to the next agent in the list (single direction, no wrap).

        Within one internal retry sequence rotation only ever moves forward by
        one position and stops once it reaches the last agent — it never wraps
        back to the first/preferred agent. "Start over from the preferred
        agent" is a per-sequence concern handled by ``_call_with_retry``'s
        entry reset (``json_retry_count == 0``), not by this method. Once the
        index is already at the last position, further failures keep running on
        the last agent (tail-on-last) until ``max_retries`` caps the sequence.

        Returns:
            True if rotation advanced to a new agent, False if already at the
            last agent (all agents exhausted for this sequence).
        """
        if self._current_agent_index + 1 >= len(self._agents):
            logger.warning("All agents exhausted — no more agents to rotate to")
            return False
        old_name = self._agents[self._current_agent_index].get("name", "?")
        self._current_agent_index += 1
        new_agent = self._agents[self._current_agent_index]
        new_name = new_agent.get("name", "?")
        logger.info(f"Rotating agent: '{old_name}' → '{new_name}' (index {self._current_agent_index})")
        self._runner = self._get_current_runner()
        return True

    def _fall_back_from_resume(self) -> None:
        """Hand a rejected native resume back to the ordinary rebuild sequence.

        INVARIANT: a rejected resume leaves the sequence able to reach EVERY
        configured agent. Planning a resume re-points the index at whichever
        agent happened to own the recorded session, and the within-sequence
        rotation only ever moves forward without wrapping — so staying on that
        index silently excluded every agent before it, and a session recorded on
        the LAST agent left rotation exhausted on the first failure, spending
        the whole budget re-running one agent. The resume was a probe that
        produced no work, so the fallback restarts the sequence where a sequence
        with no resume plan would have started: the preferred agent.
        """
        self.native_resume_rejected = True
        self._current_agent_index = 0
        self._runner = self._get_current_runner()

    def call(
        self,
        prompt: str,
        timeout: Optional[int] = None,
        context_files: Optional[List[Path]] = None,
        on_output: Optional[Callable[[str], None]] = None,
        require_json: bool = False,
        json_mode: Optional[str] = None,
        two_phase_json: bool = False,
        json_schema_hint: Optional[str] = None,
        required_keys: Optional[List[str]] = None,
        invocation_intent: AgentInvocationIntent = AgentInvocationIntent.DEFAULT,
        **kwargs,
    ) -> str:
        """Call LLM with prompt and return output text.

        Three JSON extraction modes are supported:

        1. STRICT (require_json=True, default):
           - Wraps prompt with strict JSON constraints
           - Retries up to max_retries if output is invalid
           - Best for: Simple, reliable outputs

        2. EXTRACT (json_mode="extract"):
           - Wraps prompt with JSON constraints
           - NO retries on parse failure
           - Uses LLM extraction as recovery instead
           - Best for: Balanced reliability and efficiency

        3. TWO_PHASE (json_mode="two_phase" or two_phase_json=True):
           - Clean prompt without JSON constraints
           - LLM extracts JSON from natural output
           - Best for: Complex outputs, avoiding prompt pollution

        Args:
            prompt: Main prompt text
            timeout: Deprecated, kept for API compatibility. Only inactivity timeout is used.
            context_files: Optional files to include as context
            on_output: Optional callback for real-time output
            require_json: Legacy flag for STRICT mode (kept for compatibility)
            json_mode: Explicit mode selection - "strict", "extract", "two_phase", or "off"
            two_phase_json: Legacy flag for TWO_PHASE mode (kept for compatibility)
            json_schema_hint: Optional hint about expected JSON schema for extraction
            required_keys: Optional list of keys that must be present in the parsed JSON.
                          Used by TWO_PHASE and EXTRACT modes to validate the fast path result.
            invocation_intent: Vendor-neutral purpose of the primary agent
                invocation. JSON extraction phases always use the default
                intent, so an implementation enhancement cannot leak into a
                schema-only follow-up call.
            **kwargs: Ignored (accepts model, max_tokens, temperature
                      for forward-compatibility but they don't apply
                      to claude -p subprocess calls)

        Returns:
            LLM output text (JSON if json_mode is not "off")

        Raises:
            LLMCallError: If all retries exhausted or extraction fails
        """
        if not isinstance(invocation_intent, AgentInvocationIntent):
            invocation_intent = AgentInvocationIntent(invocation_intent)

        # Resolve JSON mode from various parameter combinations
        mode = self._resolve_json_mode(json_mode, require_json, two_phase_json)

        # Each call resolves its own injection: a stale instruction from the
        # previous call must never be appended to this one's native-resume turn
        # (the rebuild path re-derives it from the prompt, so only the resume
        # path could carry it forward).
        self._pending_injected_instruction = None

        # Inject extra prompts if set (persistent for loop context, transient for Ctrl+C)
        with _extra_prompt_lock:
            injected_parts = []
            if _persistent_extra_prompt:
                injected_parts.append(_persistent_extra_prompt)
                logger.info(f"Injected persistent extra prompt: {_persistent_extra_prompt[:80]}")
            if _extra_prompt and self._taken_extra_prompt_epoch != _extra_prompt_epoch:
                injected_parts.append(_extra_prompt)
                logger.info(f"Injected transient extra prompt: {_extra_prompt[:80]}")
                # Taken for THIS caller only — a sibling DAG group's caller still
                # gets its copy; a second call by this caller does not.
                self._taken_extra_prompt_epoch = _extra_prompt_epoch
        if injected_parts:
            self._pending_injected_instruction = "\n".join(injected_parts)
            prompt = f"{prompt}\n\n[Additional user instruction]: {chr(10).join(injected_parts)}"

        # Inject read-only constraint for read-only steps
        from .context_builder import get_read_only_injection
        read_only_constraint = get_read_only_injection(
            self.step_type, force=self.force_read_only
        )
        if read_only_constraint:
            prompt = f"{prompt}{read_only_constraint}"
            logger.debug(f"Injected read-only constraint for step '{self.step_type}'")

        # Dispatch to appropriate handler based on mode
        if mode == "two_phase":
            return self._call_two_phase(
                prompt=prompt,
                timeout=timeout,
                context_files=context_files,
                on_output=on_output,
                json_schema_hint=json_schema_hint,
                required_keys=required_keys,
                invocation_intent=invocation_intent,
            )
        elif mode == "extract":
            return self._call_extract(
                prompt=prompt,
                timeout=timeout,
                context_files=context_files,
                on_output=on_output,
                json_schema_hint=json_schema_hint,
                required_keys=required_keys,
                invocation_intent=invocation_intent,
            )
        elif mode == "strict":
            return self._call_strict(
                prompt=prompt,
                timeout=timeout,
                context_files=context_files,
                on_output=on_output,
                invocation_intent=invocation_intent,
            )
        else:  # mode == "off"
            return self._call_with_retry(
                prompt=prompt,
                timeout=timeout,
                context_files=context_files,
                on_output=on_output,
                require_json=False,
                json_retry_count=0,
                invocation_intent=invocation_intent,
            )

    @staticmethod
    def _resolve_json_mode(
        json_mode: Optional[str],
        require_json: bool,
        two_phase_json: bool,
    ) -> str:
        """Resolve JSON mode from various parameter combinations.

        Priority:
        1. Explicit json_mode parameter
        2. two_phase_json=True -> "two_phase"
        3. require_json=True -> "strict"
        4. Default -> "off"
        """
        if json_mode is not None:
            mode = json_mode.lower()
            if mode in ("strict", "extract", "two_phase", "off"):
                return mode
            logger.warning(f"Unknown json_mode '{json_mode}', defaulting to 'off'")
            return "off"

        if two_phase_json:
            return "two_phase"

        if require_json:
            return "strict"

        return "off"

    def _call_strict(
        self,
        prompt: str,
        timeout: Optional[int],
        context_files: Optional[List[Path]],
        on_output: Optional[Callable[[str], None]],
        invocation_intent: AgentInvocationIntent = AgentInvocationIntent.DEFAULT,
    ) -> str:
        """Mode 1: STRICT - Force JSON with retry on failure."""
        # Wrap prompt with strict JSON constraints
        json_prompt = (
            "CRITICAL: You MUST respond with ONLY valid JSON. "
            "Do NOT include any text, explanation, or markdown before or after the JSON.\n\n"
            f"{prompt}\n\n"
            "REMINDER: Respond with ONLY the JSON object. No other text."
        )

        return self._call_with_retry(
            prompt=json_prompt,
            timeout=timeout,
            context_files=context_files,
            on_output=on_output,
            require_json=True,
            json_retry_count=0,
            invocation_intent=invocation_intent,
        )

    def _call_extract(
        self,
        prompt: str,
        timeout: Optional[int],
        context_files: Optional[List[Path]],
        on_output: Optional[Callable[[str], None]],
        json_schema_hint: Optional[str],
        required_keys: Optional[List[str]] = None,
        invocation_intent: AgentInvocationIntent = AgentInvocationIntent.DEFAULT,
    ) -> str:
        """Mode 2: EXTRACT - Request JSON, extract with LLM on failure.

        Fast path: try to leniently parse the raw output as JSON (dict or list,
        supporting markdown fences and narrative + JSON wrappers). When the fast
        path yields a dict containing all ``required_keys`` (or any dict when
        ``required_keys`` is empty / None, or a list when ``required_keys`` is
        empty / None), the parsed value is re-serialized via ``json.dumps`` and
        returned so that downstream strict ``json.loads`` consumers can read it
        directly. Otherwise falls back to the LLM-driven ``JSONExtractor``.
        """
        # Wrap prompt with JSON constraints (like STRICT)
        json_prompt = (
            "CRITICAL: You MUST respond with ONLY valid JSON. "
            "Do NOT include any text, explanation, or markdown before or after the JSON.\n\n"
            f"{prompt}\n\n"
            "REMINDER: Respond with ONLY the JSON object. No other text."
        )

        # Call without JSON retry - extraction is the recovery
        output = self._call_with_retry(
            prompt=json_prompt,
            timeout=timeout,
            context_files=context_files,
            on_output=on_output,
            require_json=False,  # Don't retry on JSON error
            json_retry_count=0,
            invocation_intent=invocation_intent,
        )

        # Fast path: lenient parse (dict with required_keys, or list when no required_keys)
        fast = self._lenient_parse_extract(output, required_keys)
        if fast is not None:
            return json.dumps(fast, ensure_ascii=False, indent=2)

        # Fallback: extract JSON via second-phase LLM call
        print(
            f"  {self.stream_prefix}[llm-caller] "
            + t("engine.llm.extract.start")
        )

        from .json_extractor import JSONExtractor

        extractor = JSONExtractor(
            project_root=self.project_root,
            timeout=300,  # 5 minutes for large outputs
        )

        result = extractor.extract(
            raw_output=output,
            schema_hint=json_schema_hint,
            required_keys=required_keys,
        )

        if result is None:
            raise LLMCallError(
                "JSON extraction failed: Could not extract valid JSON from output"
            )

        # Return as JSON string (parse_json_response will handle it)
        json_str = json.dumps(result, ensure_ascii=False, indent=2)

        print(
            f"  {self.stream_prefix}[llm-caller] "
            + t("engine.llm.extract.complete")
        )
        return json_str

    @staticmethod
    def _lenient_parse_extract(
        output: str,
        required_keys: Optional[List[str]],
    ) -> Optional[Any]:
        """Lenient JSON parse for EXTRACT fast-path.

        Returns the parsed dict or list (suitable for ``json.dumps``) when the
        fast-path contract is satisfied:
          - dict with all ``required_keys`` present (when ``required_keys`` is
            non-empty), or
          - dict or list when ``required_keys`` is empty / None.

        Returns None when the input cannot be parsed leniently, when a dict is
        missing required keys, or when the contract is mismatched (a list
        result paired with non-empty ``required_keys``, since ``required_keys``
        is dict-only).

        Handles: bare JSON value, markdown ``json`` / generic fences, narrative
        prose preceding/following the JSON, and the NDJSON multi-line stream
        format (via ``parse_json_response`` for dict input).
        """
        if not output:
            return None

        from .utils.json_parser import (
            parse_json_response,
            _try_parse_with_repairs,
        )

        # When required_keys is specified, this is a dict-only contract.
        if required_keys:
            return parse_json_response(output, required_keys=required_keys)

        text = output.strip()
        if not text:
            return None

        # Bare JSON value (dict or list) — straight repair-chain parse first.
        # This must run before any narrative-extraction heuristic, otherwise a
        # bare top-level list like '[{...}, {...}]' would be misread as the
        # final inner dict via the first '{' / last '}' fallback.
        direct = _try_parse_with_repairs(text)
        if isinstance(direct, (dict, list)):
            return direct

        # Markdown-fenced JSON: try each fenced block as a whole value.
        import re
        for pattern in (r'```json\s*([\s\S]*?)\s*```', r'```\s*([\s\S]*?)\s*```'):
            for m in re.finditer(pattern, text):
                inner = m.group(1).strip()
                inner_result = _try_parse_with_repairs(inner)
                if isinstance(inner_result, (dict, list)):
                    return inner_result

        # Narrative + bare top-level list MUST be detected before the
        # dict-via-narrative path: parse_json_response's first-'{' / last-'}'
        # (and trailing-object) heuristics would otherwise pluck the rightmost
        # inner dict from a list like [{"a":1},{"b":2}] and silently return
        # that single dict, breaking sync_discovery which expects a list.
        # Only run when the first JSON sentinel after narrative is '['.
        first_bracket = text.find('[')
        first_brace = text.find('{')
        if first_bracket != -1 and (first_brace == -1 or first_bracket < first_brace):
            list_end = text.rfind(']')
            if list_end > first_bracket:
                candidate = text[first_bracket:list_end + 1]
                cand_result = _try_parse_with_repairs(candidate)
                if isinstance(cand_result, list):
                    return cand_result

        # Dict via narrative-tolerant extraction (uses parse_json_response so
        # the existing first-'{' / last-'}' + trailing-object heuristics apply,
        # as well as NDJSON aggregation when the input is multi-line stream).
        dict_via_narrative = parse_json_response(output)
        if isinstance(dict_via_narrative, dict):
            return dict_via_narrative

        # Late fallback: list walk when dict path failed and we did not try
        # list-first above (i.e. dict sentinel preceded list sentinel but dict
        # parse did not succeed).
        if first_bracket != -1:
            list_end = text.rfind(']')
            if list_end > first_bracket:
                candidate = text[first_bracket:list_end + 1]
                cand_result = _try_parse_with_repairs(candidate)
                if isinstance(cand_result, list):
                    return cand_result

        return None

    def _get_phase1_cache_path(self) -> Optional[Path]:
        """Return the Phase 1 cache file path for this step, or None if no context."""
        if not self.flow_id or not self.step_id:
            return None
        from .chat_history import _history_dir
        return _history_dir(self.project_root, self.flow_id) / f"{self.step_id}_phase1.txt"

    def _call_two_phase(
        self,
        prompt: str,
        timeout: Optional[int],
        context_files: Optional[List[Path]],
        on_output: Optional[Callable[[str], None]],
        json_schema_hint: Optional[str],
        required_keys: Optional[List[str]] = None,
        invocation_intent: AgentInvocationIntent = AgentInvocationIntent.DEFAULT,
    ) -> str:
        """Mode 3: TWO_PHASE - Natural generation + LLM extraction.

        If phase 1 output already contains valid JSON (detected by the
        shared parse_json_response logic), skips phase 2.

        Phase 1 output is cached to disk so that if Phase 2 fails and the
        step is retried (external_attempt > 0), Phase 1 is skipped entirely
        and we go straight to Phase 2 extraction. Cache is cleared when a
        step is restarted from scratch (revision / fix-loop).
        """
        logger.info("Using two-phase JSON extraction")

        cache_path = self._get_phase1_cache_path()

        # On retry: check if Phase 1 was already completed in a previous attempt
        if self.external_attempt > 0 and cache_path and cache_path.exists():
            try:
                phase1_output = cache_path.read_text(encoding="utf-8")
                print(
                    f"  {self.stream_prefix}[llm-caller] "
                    + t("engine.llm.phase1.cached")
                )
                logger.info(f"Using cached Phase 1 output ({len(phase1_output)} chars)")
            except OSError as e:
                logger.warning(f"Failed to read Phase 1 cache, re-running Phase 1: {e}")
                phase1_output = None
        else:
            phase1_output = None

        if phase1_output is None:
            # Phase 1: Generate with clean prompt (JSON requirement is in the
            # step's prompt template itself, not added by the caller)
            phase1_output = self._call_with_retry(
                prompt=prompt,
                timeout=timeout,
                context_files=context_files,
                on_output=on_output,
                require_json=False,  # No strict JSON constraint
                json_retry_count=0,
                invocation_intent=invocation_intent,
            )

            # Persist Phase 1 output so retries can skip it
            if cache_path:
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(phase1_output, encoding="utf-8")
                    logger.info(f"Cached Phase 1 output ({len(phase1_output)} chars)")
                except OSError as e:
                    logger.warning(f"Failed to cache Phase 1 output: {e}")

        # Check if phase 1 output already contains valid JSON (skip phase 2)
        if self._contains_valid_json(phase1_output):
            from .utils.json_parser import parse_json_response
            result = parse_json_response(phase1_output, required_keys=required_keys)
            if result is not None:
                logger.info("Two-phase: phase 1 output contained valid JSON with required keys, skipping phase 2")
                print(
                    f"  {self.stream_prefix}[llm-caller] "
                    + t("engine.llm.phase1.valid_json")
                )
                # Step fully done — delete cache
                if cache_path and cache_path.exists():
                    try:
                        cache_path.unlink()
                    except OSError as e:
                        logger.warning(f"Failed to delete Phase 1 cache: {e}")
                return json.dumps(result, ensure_ascii=False, indent=2)
            else:
                logger.info("Two-phase: phase 1 JSON missing required keys %s, falling back to phase 2", required_keys)
                print(
                    f"  {self.stream_prefix}[llm-caller] "
                    + t("engine.llm.phase1.missing_keys")
                )

        # Phase 2: Extract JSON via LLM — routed through THIS caller's own
        # `_call_with_retry` instead of delegating to a fresh, default-config
        # `JSONExtractor`-spawned `LLMCaller`. This keeps Phase 2 on the same
        # configured agent chain as Phase 1, and (being a brand-new internal
        # retry sequence, json_retry_count == 0) makes the per-sequence entry
        # reset snap the agent index back to the preferred agent — so Phase 2 is
        # independent of wherever Phase 1's rotation stopped, yet still uses this
        # caller's agents rather than the global default chain.
        print(
            f"  {self.stream_prefix}[llm-caller] "
            + t("engine.llm.phase2.start")
        )

        result = self._extract_json_phase2(
            raw_output=phase1_output,
            schema_hint=json_schema_hint,
            required_keys=required_keys,
            timeout=timeout if timeout else 300,  # 5 minutes for large outputs
            context_files=context_files,
            on_output=on_output,
        )

        if result is None:
            raise LLMCallError(
                "Two-phase JSON extraction failed: Could not extract valid JSON from output"
            )

        # Phase 2 succeeded — delete the Phase 1 cache (step is fully done)
        if cache_path and cache_path.exists():
            try:
                cache_path.unlink()
            except OSError as e:
                logger.warning(f"Failed to delete Phase 1 cache after success: {e}")

        # Return as JSON string (parse_json_response will handle it)
        json_str = json.dumps(result, ensure_ascii=False, indent=2)

        print(
            f"  {self.stream_prefix}[llm-caller] "
            + t("engine.llm.extract.complete")
        )
        return json_str

    def _extract_json_phase2(
        self,
        raw_output: str,
        schema_hint: Optional[str],
        required_keys: Optional[List[str]],
        timeout: int,
        context_files: Optional[List[Path]],
        on_output: Optional[Callable[[str], None]],
    ) -> Optional[dict]:
        """Phase-2 JSON extraction that runs on THIS caller's own agent chain.

        This is the production Phase-2 path for ``_call_two_phase``. Unlike the
        legacy approach of delegating to ``JSONExtractor`` (which constructs a
        *fresh*, default-configured ``LLMCaller`` and therefore neither uses
        this caller's configured agents nor exercises its per-sequence reset),
        the extraction prompt is routed through ``self._call_with_retry`` so:

        * it runs on the *same* configured agent chain as Phase 1, and
        * being a brand-new internal retry sequence (``json_retry_count == 0``),
          the entry reset in ``_call_with_retry`` snaps ``_current_agent_index``
          back to the preferred agent — making Phase 2 independent of wherever
          Phase 1's rotation happened to stop.

        ``require_json=False`` is used deliberately: the extraction prompt
        itself demands JSON-only output, and disabling the strict JSON-retry
        recursion here avoids unbounded re-extraction. ``inject_retry_context``
        is forced to ``False`` so the self-contained extraction prompt always
        runs verbatim — this caller is reused across Phase 1/Phase 2 and across
        state-machine retries, so ``self.external_attempt`` may be > 0; without
        suppression, continue-mode retry-context injection would replace the
        extraction prompt and the extraction would yield no JSON. Returns the
        parsed dict, or ``None`` when the extraction output cannot be parsed
        (the caller maps that to ``LLMCallError``).
        """
        from .utils.json_parser import parse_json_response

        # Fast path: the Phase-1 text may already parse (mirrors the old
        # JSONExtractor.extract direct-parse shortcut). Harmless when it does
        # not — Phase 2 only runs after the required-keys check already failed.
        direct = parse_json_response(raw_output, required_keys=required_keys)
        if direct is not None:
            return direct

        from .json_extractor import EXTRACTION_PROMPT

        schema_section = (
            schema_hint
            if schema_hint
            else (
                "No specific schema provided. Structure the output to include "
                "every meaningful piece of information from the source content, "
                "using descriptive field names."
            )
        )
        extraction_prompt = EXTRACTION_PROMPT.format(
            content=raw_output,
            schema_hint=schema_section,
        )

        response = self._call_with_retry(
            prompt=extraction_prompt,
            timeout=timeout,
            context_files=context_files,
            on_output=on_output,
            require_json=False,  # extraction prompt already constrains JSON
            json_retry_count=0,  # brand-new sequence → entry reset applies
            # Run the extraction prompt verbatim. Phase 2 reuses THIS caller, so
            # self.external_attempt == the step's retry count; without this, a
            # retried two_phase step (external_attempt>0) would hit continue-mode
            # retry-context injection and drop the extraction prompt entirely.
            inject_retry_context=False,
            # INVARIANT: the extraction round-trip must not become the session a
            # later continuation resumes into. It opens its own provider session
            # whose only content is "re-express this text as JSON"; being the
            # step's newest session-bearing record, it would otherwise win
            # ``last_session_binding`` and a Retry would natively resume the
            # re-formatter rather than the agent that did the work. Tagging the
            # records makes them invisible to binding resolution while keeping
            # them in history for display.
            record_kind="extraction",
        )

        return parse_json_response(response, required_keys=required_keys)

    @staticmethod
    def _format_as_stream_json(content: str) -> str:
        """Format content as stream-json (NDJSON) format for compatibility.

        Args:
            content: The text content to format

        Returns:
            NDJSON-formatted string
        """
        message = {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": content}]
            }
        }
        return json.dumps(message, ensure_ascii=False)

    def _record_prompt(
        self,
        prompt: str,
        attempt: int,
        agent_name: Optional[str] = None,
        provider_session_id: Optional[str] = None,
        session_cwd: Optional[str] = None,
        resume_strategy: Optional[str] = None,
        runner_type: Optional[str] = None,
        kind: str = "",
    ) -> None:
        """Record a prompt to chat history if flow context is available.

        ``agent_name`` (default None) records the configuration name of the
        agent that will handle this prompt. Failures inside record_prompt are
        caught and debug-logged so metadata recording never disrupts the LLM
        call.

        INVARIANT: the session-binding arguments are supplied from the runner's
        startup metadata read BEFORE the subprocess is spawned. That ordering is
        the whole point — a call interrupted during startup must still leave a
        named, resumable provider session in history.
        """
        if not self.flow_id or not self.step_id:
            return
        try:
            from .chat_history import record_prompt
            record_prompt(
                self.project_root, self.flow_id, self.step_id,
                self.step_type, prompt, attempt,
                fix_iteration=self.fix_iteration,
                agent_name=agent_name,
                provider_session_id=provider_session_id,
                session_cwd=session_cwd,
                resume_strategy=resume_strategy,
                generation=self.generation,
                runner_type=runner_type,
                kind=kind,
            )
        except Exception as e:
            logger.debug(f"Failed to record prompt to history: {e}")

    def _record_response(
        self,
        raw_ndjson: str,
        attempt: int,
        agent_name: Optional[str] = None,
        usage_record: Optional[UsageRecord] = None,
        provider_session_id: Optional[str] = None,
        session_cwd: Optional[str] = None,
        resume_strategy: Optional[str] = None,
        runner_type: Optional[str] = None,
        kind: str = "",
    ) -> None:
        """Record an LLM response to chat history if flow context is available.

        ``agent_name`` (default None) records the configuration name of the
        agent that produced this response. Best-effort model extraction is
        handled inside ``record_response`` itself. Failures are caught and
        debug-logged so metadata recording never disrupts the LLM call.
        """
        if not self.flow_id or not self.step_id:
            return
        try:
            from .chat_history import record_response
            record_response(
                self.project_root, self.flow_id, self.step_id,
                self.step_type, raw_ndjson, attempt,
                fix_iteration=self.fix_iteration,
                agent_name=agent_name,
                usage_record=usage_record,
                provider_session_id=provider_session_id,
                session_cwd=session_cwd,
                resume_strategy=resume_strategy,
                generation=self.generation,
                runner_type=runner_type,
                kind=kind,
            )
        except Exception as e:
            logger.debug(f"Failed to record response to history: {e}")

    def _get_retry_context(self) -> Optional[str]:
        """Get previous conversation context for retry injection."""
        if not self.flow_id or not self.step_id:
            return None
        try:
            from .chat_history import format_history_for_retry
            return format_history_for_retry(
                self.project_root, self.flow_id, self.step_id,
                mode=self.retry_mode,
                current_fix_iteration=self.fix_iteration,
                current_generation=self.generation,
            )
        except Exception as e:
            logger.warning(f"Failed to get retry context (falling back to original prompt): {e}")
            return None

    # ------------------------------------------------------------------
    # Native session resume — strategy selection lives HERE, never in a runner
    # ------------------------------------------------------------------

    def _take_dialog_resume(self) -> bool:
        """Per-caller view of the process-wide post-dialog continuation flag.

        Epoch-based rather than consuming: a DAG IMPLEMENT step resumes several
        groups in parallel and every one of them was interrupted, so every one
        needs the "you were interrupted, the discussion has concluded" framing.
        A global consume gave it to whichever thread arrived first.
        """
        with _extra_prompt_lock:
            if not _dialog_resume_pending:
                return False
            if self._taken_dialog_resume_epoch == _dialog_resume_epoch:
                return False
            self._taken_dialog_resume_epoch = _dialog_resume_epoch
            return True

    def _agent_index_for_binding(self, binding: Dict[str, Any]) -> Optional[int]:
        """Locate the recorded session's agent in the CURRENT agents list.

        INVARIANT: BOTH the recorded agent name and the recorded runner type
        must still match. A session id is only meaningful together with the
        runner that owns it, so a config edit that re-pointed the same agent
        name at a different runner type must invalidate the binding rather than
        hand a Claude session id to codex. A record carrying no runner type at
        all (legacy jsonl written before the field existed) is likewise
        unusable: "the name still matches" is not evidence about the provider,
        and rebuilding context is the safe, always-correct alternative.
        """
        name = binding.get("agent_name")
        if not name:
            return None
        recorded_type = str(binding.get("runner_type") or "")
        if not recorded_type:
            logger.info(
                "Native resume unavailable: the recorded session for agent %r "
                "carries no runner type; rebuilding context instead", name,
            )
            return None
        for index, agent in enumerate(self._agents):
            if agent.get("name") != name:
                continue
            if str(agent.get("type", "claude-code")) != recorded_type:
                return None
            return index
        return None

    def _plan_native_resume(self) -> Optional[Dict[str, Any]]:
        """Decide whether this attempt can continue a recorded provider session.

        Returns ``{"agent_index", "session_id", "cwd"}`` when every condition
        holds — configured strategy is ``native``, a session id was recorded for
        this step/iteration/generation, its agent entry still exists, and that
        agent's runner declares (and has been verified to have) native resume —
        otherwise ``None``, which routes the attempt down the rebuild path.

        Never raises: an unreadable history is a reason to rebuild, not to fail
        the call.
        """
        if self.resume_strategy != "native":
            return None
        if not self.flow_id or not self.step_id:
            return None
        return self._plan_from_history()

    def _plan_explicit_resume(self) -> Optional[Dict[str, Any]]:
        """Resolve the caller-supplied ``resume_binding`` into a resume plan."""
        binding = self._resume_binding
        if not binding or not binding.get("provider_session_id"):
            return None
        index = self._agent_index_for_binding(binding)
        if index is None:
            return None
        try:
            runner = self._create_runner_cached(index)
        except Exception:  # pragma: no cover - unknown agent type
            logger.debug("Failed to build runner for explicit resume", exc_info=True)
            return None
        if not getattr(runner, "supports_native_resume", False):
            return None
        return {
            "agent_index": index,
            "session_id": binding["provider_session_id"],
            "cwd": Path(binding.get("session_cwd") or self.project_root),
            "verbatim_prompt": True,
        }

    def _plan_from_history(self) -> Optional[Dict[str, Any]]:
        try:
            from .chat_history import last_session_binding

            binding = last_session_binding(
                self.project_root, self.flow_id, self.step_id,
                fix_iteration=self.fix_iteration,
                generation=self.generation,
            )
        except Exception:  # pragma: no cover - defensive
            logger.debug("Failed to read session binding", exc_info=True)
            return None
        if not binding or not binding.get("provider_session_id"):
            return None
        index = self._agent_index_for_binding(binding)
        if index is None:
            logger.info(
                "Native resume unavailable: recorded agent %r is no longer in "
                "the agent chain; rebuilding context instead",
                binding.get("agent_name"),
            )
            return None
        try:
            runner = self._create_runner_cached(index)
        except Exception:  # pragma: no cover - unknown agent type
            logger.debug("Failed to build runner for resume", exc_info=True)
            return None
        if not getattr(runner, "supports_native_resume", False):
            return None
        # INVARIANT: an implicitly planned resume runs in THIS caller's
        # workspace, never in the directory the record happens to name. A
        # session whose cwd is some other tree (a DAG group worktree that was
        # rejected for reuse and rebuilt elsewhere, and whose old directory
        # still exists) would otherwise be resumed there — the agent would edit
        # the wrong checkout while this caller's branch stayed empty, and the
        # group would report success having contributed nothing. Only an
        # EXPLICIT resume binding may cross workspaces, because its caller
        # chose the session deliberately.
        recorded_cwd = binding.get("session_cwd")
        if recorded_cwd and not _same_directory(recorded_cwd, self.project_root):
            logger.info(
                "Native resume unavailable: session %s is bound to %s, not this "
                "workspace (%s); rebuilding context instead",
                binding.get("provider_session_id"), recorded_cwd, self.project_root,
            )
            return None
        return {
            "agent_index": index,
            "session_id": binding["provider_session_id"],
            "cwd": Path(recorded_cwd or self.project_root),
        }

    def _create_runner_cached(self, index: int) -> AgentRunner:
        """Return (and cache) the runner for the agent at *index*."""
        agent = self._agents[index]
        cache_key = agent.get("name", agent.get("cmd", str(index)))
        if cache_key not in self._runner_cache:
            self._runner_cache[cache_key] = self._create_runner(agent)
        return self._runner_cache[cache_key]

    def _build_resume_prompt(
        self,
        original_prompt: str,
        require_json: bool,
        dialog_resume: bool,
        directive: Optional[str] = None,
    ) -> str:
        """Compose the single user turn a native resume appends to the session.

        The provider still holds the whole conversation, so this carries NO
        rebuilt context — only what changed since the agent stopped: why it was
        stopped, what to do first, any instruction the user settled on in the
        dialog, and a restatement of the step's output contract (the JSON shape
        in particular, which the agent last saw many turns ago and which the
        step's parser hard-depends on).
        """
        lead = _DIALOG_RESUME_FRAMING if dialog_resume else (
            "[Continuation]\n"
            "Continue this task from where you left off. Do NOT repeat "
            "work already completed. If your previous attempt failed, fix "
            "the cause and carry on rather than starting over."
        )
        # dialog_resume=False here: the framing is already this prompt's lead.
        return "\n\n".join(
            [lead]
            + self._continuation_addenda(
                original_prompt,
                require_json,
                dialog_resume=False,
                directive=directive,
            )
        )

    def _continuation_addenda(
        self,
        original_prompt: str,
        require_json: bool,
        *,
        dialog_resume: bool,
        directive: Optional[str] = None,
    ) -> List[str]:
        """Everything a continuation must carry beyond "keep going".

        INVARIANT: shared by BOTH continuation strategies. ``rebuild`` differs
        from ``native`` only in how the earlier conversation is supplied — it
        must never drop what the user decided. The dialog's temporary
        instruction (and the "the task description was replaced" notice that
        travels with it) reaches the agent through this list on the rebuild
        path too, where the assembled ``prompt`` carrying them is NOT sent.
        """
        parts: List[str] = []
        if dialog_resume:
            parts.append(_DIALOG_RESUME_FRAMING)
        if directive:
            parts.append(directive)
        instruction = self._pending_injected_instruction
        if instruction:
            parts.append(f"[Additional user instruction]: {instruction}")
        contract = self._output_contract_reminder(original_prompt, require_json)
        if contract:
            parts.append(contract)
        return parts

    @staticmethod
    def _output_contract_reminder(
        original_prompt: str, require_json: bool
    ) -> Optional[str]:
        """Restate the step's output contract for a resumed turn.

        A resumed turn is a brand-new user message in a long session; the
        agent's last sight of the required response shape may be far behind it.
        The step's parser is unforgiving, so the contract is restated verbatim
        where the prompt declared it as a fenced ``json`` block, and generically
        otherwise.
        """
        json_required = require_json or "ONLY valid JSON" in (original_prompt or "")
        fence = None
        if original_prompt:
            # Only the step's OWN prompt body may declare the contract; the
            # framework injections that follow it are reference material.
            scope = original_prompt
            for header in _POST_CONTRACT_INJECTION_HEADERS:
                cut = scope.find(header)
                if cut > 0:
                    scope = scope[:cut]
            marker = "```json"
            start = scope.rfind(marker)
            if start >= 0:
                end = scope.find("```", start + len(marker))
                if end > start:
                    fence = scope[start : end + 3]
        if fence:
            return (
                "[Output contract — unchanged]\n"
                "Your reply for this step must still match this shape:\n" + fence
            )
        if json_required:
            return (
                "[Output contract — unchanged]\n"
                "Reply with ONLY the JSON object this step requires. No prose "
                "before or after it."
            )
        return None

    def _add_deny_shell(self, kwargs: Dict[str, Any], builder: Any) -> None:
        """Add ``deny_shell=True`` to *kwargs* when this caller is strict.

        Introspected rather than passed unconditionally, exactly like
        ``invocation_intent``: a runner written against the pre-strict interface
        stays a valid adapter. It cannot enforce the boundary, so the gap is
        logged rather than passed over silently.
        """
        if not getattr(self, "deny_shell", False):
            return
        import inspect

        try:
            params = inspect.signature(builder).parameters.values()
            accepts = any(
                p.name == "deny_shell" or p.kind == inspect.Parameter.VAR_KEYWORD
                for p in params
            )
        except (TypeError, ValueError):  # pragma: no cover - builtins/C callables
            accepts = False
        if accepts:
            kwargs["deny_shell"] = True
        else:
            logger.warning(
                "Runner %s cannot express the strict read-only lock; the call "
                "keeps only the edit-tool denial",
                type(getattr(builder, "__self__", builder)).__name__,
            )

    def _call_with_retry(
        self,
        prompt: str,
        timeout: int,
        context_files: Optional[List[Path]],
        on_output: Optional[Callable[[str], None]],
        require_json: bool,
        json_retry_count: int,
        max_json_retries: int = 2,
        inject_retry_context: bool = True,
        invocation_intent: AgentInvocationIntent = AgentInvocationIntent.DEFAULT,
        continuation_directive: Optional[str] = None,
        record_kind: str = "",
    ) -> str:
        """Internal method to call LLM with retry and agent rotation logic.

        Agent-rotation semantics span three layers:

        * **Per-sequence reset (this method's entry).** Each fresh internal
          retry sequence starts over from the first/preferred agent. When
          ``json_retry_count == 0`` (i.e. this is a brand-new sequence, not a
          JSON continuation), ``_current_agent_index`` is reset to ``0`` and
          ``self._runner`` is refreshed before the attempt loop. This is what
          guarantees every ``call()`` — including each ``_call_two_phase``
          phase and every cross-round reuse of a shared ``LLMCaller`` — begins
          on the preferred model rather than wherever the previous sequence's
          rotation happened to stop.
        * **Within-sequence rotation (single direction, tail-on-last).** On a
          failure the loop calls ``_rotate_agent`` which advances the index by
          one and stops once it reaches the last agent (it never wraps). If
          ``max_retries`` exceeds the agent count, the surplus attempts run on
          the last agent until ``max_retries`` caps the sequence and raises.
        * **JSON continuation (no reset).** The recursive self-call at the
          ``require_json`` non-JSON path (``json_retry_count > 0``) is a
          session continuation of the *same* logical call, so it deliberately
          skips the reset and keeps the current agent and conversation context.

        ``inject_retry_context`` (default ``True``) controls whether the
        chat-history retry-context block is prepended on retries. The Phase-2
        extraction path (``_extract_json_phase2``) passes ``False``: its
        ``extraction_prompt`` is self-contained (it embeds the raw content and
        schema and demands JSON-only output) and MUST run verbatim. Because
        Phase 2 reuses *this* caller instance, ``self.external_attempt`` equals
        the step's state-machine retry count; with injection enabled and the
        default ``retry_mode == "continue"`` a retried two_phase step would
        discard the extraction prompt and tell the model to "continue the
        task", yielding no JSON. Suppressing injection here faithfully
        reproduces the legacy fresh-``LLMCaller`` (``external_attempt == 0``,
        no flow/step context) behavior that always ran the extraction prompt
        as-is.
        """
        original_prompt = prompt

        # Reset touched-files tracking for this call
        self._last_touched_files = set()

        # Per-sequence agent-rotation reset: every NEW internal retry sequence
        # starts over from the first (preferred) agent. json_retry_count == 0
        # marks a brand-new sequence; the JSON-continuation recursion
        # (json_retry_count > 0, triggered below when require_json output is
        # not valid JSON) intentionally does NOT reset, so it keeps the current
        # agent and the in-progress conversation context. No wrap-around /
        # modulo logic — within-sequence rotation stays single-direction and
        # tail-on-last (see _rotate_agent).
        if json_retry_count == 0:
            self._current_agent_index = 0
            self._runner = self._get_current_runner()

        env = dict(os.environ)
        env.pop("CLAUDECODE", None)

        start_time = time.time()
        # One entry per attempt. Rotation consumes an attempt slot and
        # ``continue``s past the tail-on-last error assignment, so a sequence
        # that rotates through every agent used to end with an empty reason —
        # the final LLMCallError said only "failed after N attempts: ".
        # Recording before the rotation branch keeps every attempt accounted for.
        attempt_errors: List[str] = []
        sequence_call_id = uuid.uuid4().hex
        # Set once a native resume fails for any reason; every later attempt in
        # this sequence rebuilds instead. A resume failure is never fatal — it
        # is a reason to fall back, per decision 1.
        native_resume_disabled = False
        # Framing for the continuation directive, consumed once per sequence so
        # a retry inside the same sequence does not re-announce the dialog.
        dialog_resume = self._take_dialog_resume()
        # A native resume that the provider refuses is a PROBE, not an attempt:
        # it says nothing about the agent's health and produced no work. Letting
        # it eat one of ``max_retries`` meant a rejected resume on agent A could
        # leave healthy agent C untried (and, at max_retries=1, meant no rebuild
        # ever ran). Each such fallback therefore hands the slot back, and
        # ``_fall_back_from_resume`` puts the index back where a sequence with no
        # resume plan would have started. At most one can occur per sequence —
        # the fallback also disables further resume planning — so the budget
        # stays bounded.
        resume_fallback_slots = 0

        internal_attempt = -1
        while True:
            internal_attempt += 1
            attempt_budget = self.max_retries + resume_fallback_slots
            if internal_attempt >= attempt_budget:
                break
            # Cooperative stop, checked before spawning anything: a stop that
            # arrived while the previous attempt was winding down must not be
            # answered by starting a fresh subprocess the user just asked us to
            # stop. Raising here is safe — this runs on the calling thread, and
            # the DAG scheduler converts a worker-thread raise into a group
            # result rather than losing it.
            if get_stop_signal().is_set():
                logger.info("Stop requested before attempt %d; aborting sequence",
                            internal_attempt + 1)
                raise KeyboardInterrupt

            # ``is_retry`` gates retry-context injection AND dedup. Phase-2
            # extraction passes inject_retry_context=False so its self-contained
            # extraction prompt always runs verbatim, regardless of the step's
            # external retry count (see this method's docstring).
            is_retry = inject_retry_context and (
                self.external_attempt > 0 or internal_attempt > 0
            )

            # Native resume is considered for exactly the situations that would
            # otherwise inject a rebuilt retry context (post-dialog continue,
            # failure retry, --resume of a RUNNING/FAILED step, JSON retry,
            # internal attempt retry). Selecting it re-points the sequence at
            # the agent that owns the session — rotation stays LLMCaller's
            # decision, never the runner's.
            resume_plan = None
            if not native_resume_disabled:
                if self._resume_binding is not None:
                    # An explicit binding is a direct instruction to talk to a
                    # specific session — it applies on the very first attempt,
                    # not only on retries.
                    resume_plan = self._plan_explicit_resume()
                elif is_retry:
                    resume_plan = self._plan_native_resume()
            if resume_plan is not None:
                self._current_agent_index = resume_plan["agent_index"]
                self._runner = self._get_current_runner()
            elif self._resume_binding is not None:
                # The caller asked to talk to a SPECIFIC session and this
                # attempt is not doing so — the recorded agent was removed, its
                # type was re-pointed, or its runner cannot resume. Whoever
                # answers now is a standalone interlocutor, and the caller has
                # to know: the interjection dialog would otherwise keep
                # announcing "talking to <agent> in its own session" and keep
                # stamping its history records with a session id it is not
                # addressing.
                self.native_resume_rejected = True

            # Snapshot the current agent name at the start of this attempt.
            # This captures the agent BEFORE any rotation that might occur
            # during this attempt's failure path, so both prompt and response
            # records for this attempt carry the same agent attribution.
            attempt_agent_name = self._agents[self._current_agent_index].get("name", "?")
            attempt_agent = self._agents[self._current_agent_index]
            attempt_runner_type = str(attempt_agent.get("type", "claude-code"))
            attempt_provider = attempt_agent.get("provider")
            attempt_model = attempt_agent.get("model")
            configured_model = expand_configured_model(attempt_model, env)
            attempt_call_id = f"{sequence_call_id}:attempt:{internal_attempt}"
            active_tracker: Optional[StreamJSONTracker] = None
            active_session_relay: Optional[SessionCaptureRelay] = None

            def attempt_session_id() -> Optional[str]:
                """Session identity this attempt actually ran under.

                INVARIANT: every record of one attempt names the same session.
                A pre-allocating adapter's id is known before the spawn, so the
                startup metadata is authoritative there; a capture-only provider
                mints its own mid-stream, so anything written after the run must
                read it back from whichever observer saw it — the tracker on the
                default path, the relay when the caller supplied ``on_output`` —
                instead of persisting the empty pre-spawn seed.
                """
                if startup_metadata.provider_session_id:
                    return startup_metadata.provider_session_id
                if active_tracker is not None and active_tracker.session_id:
                    return active_tracker.session_id
                if active_session_relay is not None:
                    return active_session_relay.session_id
                return None

            active_call_id = attempt_call_id
            active_usage_recorded = False
            active_response_recorded = False
            active_output = ""
            # True once the resumed turn has actually produced (and recorded) a
            # result. WHY it gates the resume-rejection classification below: an
            # exception raised AFTER the provider answered — the nested JSON
            # retry exhausting its budget, a post-processing fault — says
            # nothing about whether the resume was accepted. Treating it as a
            # rejection dropped a session binding the provider never refused and
            # granted an extra attempt slot on top of ``max_retries``.
            resume_produced_result = False
            startup_metadata = RunnerStartupMetadata()
            startup_model: Optional[str] = None
            effective_provider = attempt_provider

            # Announce this attempt's agent (model not yet known) so a consumer
            # — e.g. the DAG-parallel implement group closure — can show the
            # real agent the moment the attempt starts, and so each rotation /
            # retry surfaces its own agent rather than sticking with a stale
            # name. The model upgrade follows from the tracker once parsed.
            self._notify_agent_change(attempt_agent_name, None)

            # On retry (either external or internal), inject previous conversation context
            if resume_plan is not None:
                # Native resume: the provider still holds the conversation, so
                # the prompt is ONLY the new user turn. No retry context, no
                # dedup, no safety cap — there is nothing rebuilt to bound.
                effective_prompt = (
                    prompt
                    if resume_plan.get("verbatim_prompt")
                    else self._build_resume_prompt(
                        original_prompt,
                        require_json,
                        dialog_resume,
                        directive=continuation_directive,
                    )
                )
            elif (
                self._resume_binding is not None
                and self._resume_fallback_prompt
            ):
                # An explicit resume binding was asked for but is not being
                # honoured on this attempt (unsupported runner, removed agent,
                # or a resume that already failed). The interlocutor answering
                # now holds none of that session's context, so it gets the
                # caller's self-contained fallback prompt instead of the
                # session-relative one. Checked BEFORE the retry branch: such a
                # caller owns its own context assembly, and the generic rebuilt
                # retry context is not what it asked for.
                effective_prompt = self._resume_fallback_prompt
            elif is_retry:
                retry_context = self._get_retry_context()
                if retry_context:
                    if self.retry_mode == "continue":
                        # In continue mode, the original prompt is already in the history.
                        # Append a short continuation instruction instead of re-prepending the full prompt.
                        # The addenda are NOT optional here: on this path the
                        # assembled ``prompt`` (which carries the dialog's
                        # instruction and the revised-description notice) is
                        # never sent, so dropping them would silently discard
                        # the user's decision whenever the strategy falls back
                        # to rebuild.
                        effective_prompt = "\n\n".join(
                            [
                                f"{retry_context}\n"
                                "Continue the task from where you left off based on "
                                "the conversation history above. Do NOT repeat work "
                                "already completed."
                            ]
                            + self._continuation_addenda(
                                original_prompt,
                                require_json,
                                dialog_resume=dialog_resume,
                                directive=continuation_directive,
                            )
                        )
                    else:
                        # In retry mode, prepend history + original prompt (old behavior)
                        effective_prompt = f"{retry_context}\n{original_prompt}"
                else:
                    effective_prompt = original_prompt
            else:
                effective_prompt = prompt

            # Deduplicate repeated line blocks (e.g. spec content repeated across retry attempts).
            # Only on retries — first call has no internal repetition by definition.
            if is_retry and resume_plan is None:
                # Convert literal two-char ``\n`` escape sequences (left over from
                # JSON-encoded tool_result previews in the retry-context body)
                # into real newlines BEFORE dedup. Without this, multi-line file
                # content embedded in tool previews stays as single huge "lines"
                # to ``str.split("\n")`` and dedup misses it. Limit to ``\n``
                # only — leaving ``\t`` / ``\\`` / ``\"`` intact since they don't
                # affect line-level dedup and re-interpreting them risks garbling
                # legitimate code samples.
                effective_prompt = effective_prompt.replace('\\n', '\n')
                try:
                    effective_prompt = deduplicate_prompt_lines(effective_prompt)
                except Exception:
                    logger.warning("deduplicate_prompt_lines failed, using original prompt", exc_info=True)
                effective_prompt = _post_dedup_safety_cap(effective_prompt)

            # INVARIANT: the runner's startup metadata is read BEFORE the prompt
            # is recorded, so the attempt's provider session id lands in history
            # ahead of the subprocess it identifies. The reverse order (the
            # historical one) left an interrupted-at-startup call with a live
            # provider session that no record named, and therefore no resume
            # could ever address.
            current_runner = self._get_current_runner()
            current_agent_name = self._agents[self._current_agent_index].get("name", "?")
            if resume_plan is None:
                try:
                    startup_metadata = current_runner.get_startup_metadata(env)
                except Exception:
                    logger.debug(
                        "Runner startup metadata unavailable for %s",
                        attempt_agent_name,
                        exc_info=True,
                    )
                    startup_metadata = RunnerStartupMetadata()
                if not isinstance(startup_metadata, RunnerStartupMetadata):
                    startup_metadata = RunnerStartupMetadata()
            else:
                # Resuming: the session identity is the recorded one, not a
                # freshly minted one. Calling get_startup_metadata here would
                # allocate a NEW id on the Claude adapters and silently detach
                # the record from the session actually being continued.
                startup_metadata = RunnerStartupMetadata(
                    provider=getattr(current_runner, "startup_provider", None),
                    model=getattr(current_runner, "startup_model", None),
                    provider_session_id=resume_plan["session_id"],
                )
            effective_provider = attempt_provider or startup_metadata.provider
            startup_model = expand_configured_model(startup_metadata.model, env)
            attempt_strategy = "native" if resume_plan is not None else "rebuild"
            attempt_cwd = (
                resume_plan["cwd"] if resume_plan is not None else self.project_root
            )

            # Record the original prompt (NOT effective_prompt) to chat history.
            # effective_prompt on retries contains the retry-context block (marker..separator).
            # If we recorded that, the next retry's format_history_for_retry would read it back
            # as a user message and re-embed it inside a fresh retry-context, producing
            # second-order recursive bloat across attempts. Recording original_prompt keeps
            # the persistent record clean — the retry-context is rebuilt from history each call.
            #
            # EXCEPTION for a native resume: the original prompt was never sent
            # to the provider on this attempt, only the short continuation turn
            # was. Recording the continuation turn is what makes the jsonl a
            # truthful record of the conversation the session actually holds.
            self._record_prompt(
                effective_prompt if resume_plan is not None else original_prompt,
                self.external_attempt,
                agent_name=attempt_agent_name,
                provider_session_id=startup_metadata.provider_session_id,
                session_cwd=str(attempt_cwd),
                resume_strategy=attempt_strategy,
                runner_type=attempt_runner_type,
                kind=record_kind,
            )

            try:
                import inspect

                try:
                    build_params = inspect.signature(
                        current_runner.build_call_args
                    ).parameters.values()
                    accepts_intent = any(
                        p.name == "invocation_intent"
                        or p.kind == inspect.Parameter.VAR_KEYWORD
                        for p in build_params
                    )
                except (TypeError, ValueError):
                    accepts_intent = False

                # Delegate CLI argument construction to the runner.  Each
                # runner translates the caller's intent (prompt, read-only
                # flag, context files) into its own agent-specific CLI flags.
                from .context_builder import is_step_read_only
                # force_read_only ORs on top of the registry decision: a step
                # that writes files (read_only=False) can still hold this LLM
                # sub-call read-only, so the runner emits its --disallowedTools
                # tool-level lock. Never the reverse — a read-only step cannot be
                # forced writable here.
                read_only_call = (
                    is_step_read_only(self.step_type) or self.force_read_only
                )
                if resume_plan is not None:
                    resume_kwargs = dict(
                        session_id=resume_plan["session_id"],
                        prompt=effective_prompt,
                        read_only=read_only_call,
                        context_files=context_files,
                    )
                    self._add_deny_shell(
                        resume_kwargs, current_runner.build_resume_call_args
                    )
                    args = current_runner.build_resume_call_args(**resume_kwargs)
                    logger.info(
                        "Resuming provider session %s for step '%s' with agent "
                        "'%s' (cwd=%s)",
                        resume_plan["session_id"], self.step_type,
                        current_agent_name, attempt_cwd,
                    )
                else:
                    build_kwargs = dict(
                        prompt=effective_prompt,
                        read_only=read_only_call,
                        context_files=context_files,
                    )
                    self._add_deny_shell(
                        build_kwargs, current_runner.build_call_args
                    )
                    if invocation_intent != AgentInvocationIntent.DEFAULT:
                        # Third-party runners compiled against the pre-intent
                        # interface remain valid ordinary direct executors. A
                        # runner that wants native enhancement opts in by
                        # accepting the new keyword (or **kwargs) and declaring
                        # capability.
                        if accepts_intent:
                            build_kwargs["invocation_intent"] = invocation_intent
                    args = current_runner.build_call_args(**build_kwargs)
                logger.debug(
                    f"LLM call internal_attempt {internal_attempt + 1}/{self.max_retries}, "
                    f"external_attempt {self.external_attempt}, agent '{current_agent_name}'"
                )

                # Capture CLI-subprocess confirmation prompts emitted by the
                # child Claude process (e.g. "按 1 确定") and surface them as a
                # ``cli_confirm`` interaction call file so the web console can
                # answer them — otherwise the child would block on stdin and
                # stall the whole flow.
                from .interaction_calls import make_cli_confirm_handler

                on_confirm = make_cli_confirm_handler(
                    self.project_root,
                    flow_id=self.flow_id,
                    step_id=self.step_id,
                )

                stream_tracker = None
                if on_output:
                    # The caller renders the stream itself, but session identity
                    # is not the caller's concern — relay through the capture
                    # sidecar so a capture-only provider's id still lands on
                    # THIS attempt's prompt record the moment the stream
                    # announces it (see SessionCaptureRelay).
                    session_relay = SessionCaptureRelay(
                        on_output,
                        project_root=self.project_root,
                        flow_id=self.flow_id,
                        step_id=self.step_id,
                        attempt=self.external_attempt,
                        agent_name=attempt_agent_name,
                        seed_session_id=startup_metadata.provider_session_id,
                    )
                    active_session_relay = session_relay
                    result = current_runner.run_with_monitor(
                        args=args,
                        wall_timeout=None,  # No wall time limit, only inactivity timeout
                        inactivity_timeout=1800,  # 30 minutes
                        # A provider session is bound to the cwd it was opened
                        # in, so a resume MUST be issued from the recorded one
                        # (a DAG group's session lives in its own worktree).
                        cwd=attempt_cwd,
                        env=env,
                        on_output=session_relay,
                        on_confirm=on_confirm,
                    )
                else:
                    set_project_root(self.project_root)
                    # Pass flow context so the tracker can flush in-progress
                    # process lines to the step jsonl BEFORE the final result is
                    # recorded — the daemon's incremental reader then forwards
                    # them so the web console shows the running step line by line.
                    # Only step paths with flow_id/step_id (i.e. caller.call from
                    # the state machine) write progress; ad-hoc callers (no
                    # flow_id/step_id) keep the prior stdout-only behavior. The
                    # on_output!=None branch above is unchanged: no current step
                    # routes through it.
                    stream_tracker = StreamJSONTracker(
                        call_id=attempt_call_id,
                        stream_prefix=self.stream_prefix,
                        project_root=self.project_root,
                        flow_id=self.flow_id,
                        step_id=self.step_id,
                        step_type=self.step_type,
                        attempt=self.external_attempt,
                        usage_attempt=internal_attempt,
                        # Same agent name used for this attempt's prompt/response
                        # records, so the streamed progress lines, the prompt,
                        # and the response all agree on the agent that actually
                        # ran — and a rotation/retry's fresh tracker carries the
                        # new agent rather than the stale one.
                        agent_name=attempt_agent_name,
                        runner_type=attempt_runner_type,
                        provider=effective_provider,
                        provider_session_id=startup_metadata.provider_session_id,
                        configured_model=configured_model,
                        runner_startup_model=startup_model,
                        # Let the tracker upgrade the consumer's label to
                        # "agent · model" once the model name is parsed from
                        # the stream's init/system metadata.
                        on_agent_change=self._notify_agent_change,
                    )
                    active_tracker = stream_tracker

                    # Seed the accumulating bubble with an identity-only record
                    # so the current reply area shows the real agent the moment
                    # this attempt starts — before any text/tool fragment (or a
                    # call that only returns a final result) would otherwise make
                    # it visible. Retries / rotations build a fresh tracker, so
                    # each attempt seeds its own agent.
                    stream_tracker.emit_agent_identity()

                    def on_stream_output(line: str) -> None:
                        stream_tracker.process_line(line)

                    result = current_runner.run_with_monitor(
                        args=args,
                        wall_timeout=None,  # No wall time limit, only inactivity timeout
                        inactivity_timeout=1800,  # 30 minutes
                        # See above: sessions are cwd-bound, so a resume runs
                        # in the cwd recorded with the session.
                        cwd=attempt_cwd,
                        env=env,
                        on_output=on_stream_output,
                        on_confirm=on_confirm,
                    )

                    active_output = result.output or ""
                    if result.success:
                        stream_tracker.print_summary()
                        self._last_touched_files = stream_tracker.touched_files
                    else:
                        self._last_touched_files = set()

                    # Fold this subprocess's token usage into the current step
                    # accumulator (best-effort). Done on BOTH success and
                    # failure paths so retry / rotation attempts each count
                    # against the step total; covered by add_call_usage's own
                    # no-op-when-out-of-scope + swallow-all contract, so this
                    # never disrupts the LLM call regardless of flow context.
                active_output = result.output or ""
                if stream_tracker is not None:
                    attempt_usage = stream_tracker.usage_record
                else:
                    attempt_usage = parse_usage_record(
                        result.output or "",
                        call_id=attempt_call_id,
                        attempt=internal_attempt,
                        agent_name=attempt_agent_name,
                        runner_type=attempt_runner_type,
                        provider=effective_provider,
                        provider_session_id=attempt_session_id(),
                        configured_model=configured_model,
                        runner_startup_model=startup_model,
                    )
                active_output = result.output or ""
                add_call_usage(attempt_usage)
                active_usage_recorded = True

                # Record the response (whether success, failure, or interrupted)
                self._record_response(
                    result.output or "",
                    self.external_attempt,
                    agent_name=attempt_agent_name,
                    usage_record=attempt_usage,
                    provider_session_id=attempt_session_id(),
                    session_cwd=str(attempt_cwd),
                    resume_strategy=attempt_strategy,
                    runner_type=attempt_runner_type,
                    kind=record_kind,
                )
                active_response_recorded = True
                resume_produced_result = resume_plan is not None

                # Extract the type: "result" message's text for callers that
                # need the full LLM output (e.g. discovery multi-turn context)
                self.last_raw_result = self._extract_result_text(result.output or "")

                # If interrupted by Ctrl+C, re-raise after saving partial output
                if isinstance(getattr(result, 'interrupted', False), bool) and result.interrupted:
                    logger.info("LLM call interrupted by user, partial output saved to history")
                    raise KeyboardInterrupt

                if result.success:
                    # When require_json=False, extract text content from NDJSON
                    # so callers get usable text instead of raw stream-json output
                    if not require_json and result.output:
                        extracted = self._extract_text_from_ndjson(result.output)
                        if extracted:
                            result.output = extracted

                    # Check if JSON is required but not received
                    if require_json and json_retry_count < max_json_retries:
                        if not self._contains_valid_json(result.output):
                            print(
                                f"  {self.stream_prefix}[llm-caller] "
                                + t(
                                    "engine.llm.json_retry",
                                    attempt=json_retry_count + 1,
                                    max_retries=max_json_retries,
                                )
                            )
                            json_prompt = self._create_json_retry_prompt(prompt, result.output)
                            # The corrective instruction travels as the
                            # continuation directive, not only inside the
                            # rebuilt prompt: a continuation (native resume or
                            # ``retry_mode: continue``) sends neither the
                            # original prompt nor ``json_prompt``, so without
                            # this the agent would be told merely to "continue"
                            # and never learn its reply was not JSON.
                            #
                            # WHY no prompt record is written here: the
                            # recursive call records what it ACTUALLY sends,
                            # with this attempt's session/strategy fields. A
                            # record written now would claim a prompt that no
                            # strategy necessarily delivers.
                            json_directive = self._create_json_retry_directive(
                                result.output
                            )
                            # Increment external_attempt to ensure retry context is injected
                            # This is crucial because JSON retry needs the previous conversation context
                            # (including tool calls/results) to avoid re-reading files
                            self.external_attempt += 1
                            return self._call_with_retry(
                                prompt=json_prompt,
                                timeout=timeout,
                                context_files=context_files,
                                on_output=on_output,
                                require_json=require_json,
                                json_retry_count=json_retry_count + 1,
                                max_json_retries=max_json_retries,
                                invocation_intent=AgentInvocationIntent.DEFAULT,
                                continuation_directive=json_directive,
                                record_kind=record_kind,
                            )

                    duration_s = time.time() - start_time
                    logger.debug(f"LLM call succeeded in {int(duration_s * 1000)}ms")
                    return result.output

                # --- Native-resume fallback, BEFORE any rotation ---
                # A resume can fail for reasons that say nothing about the
                # agent: the session was pruned provider-side, the flag is
                # unsupported by an older CLI, the transcript ends on a
                # tool_use the provider refuses to continue. None of those are
                # a reason to rotate away — they are a reason to rebuild
                # context and run the sequence as if no resume had been planned.
                if resume_plan is not None:
                    logger.warning(
                        "Native resume of session %s failed (exit=%s) on agent "
                        "'%s'; falling back to rebuilt context",
                        resume_plan["session_id"], result.returncode,
                        current_agent_name,
                    )
                    attempt_errors.append(
                        f"attempt {internal_attempt + 1}: native resume of "
                        f"session {resume_plan['session_id']} failed "
                        f"(exit={result.returncode}) — falling back to rebuild"
                    )
                    native_resume_disabled = True
                    resume_fallback_slots += 1
                    self._fall_back_from_resume()
                    continue

                # --- Failure path: always attempt agent rotation ---
                # detect_infra_error is retained for diagnostic labeling only;
                # USAGE_LIMIT / TIMEOUT / OTHER all trigger rotation identically.
                # Pass stderr_tail when available (CodexRunner and
                # ClaudeCodeRunner populate it; other result shapes fall back
                # to "" via getattr).
                infra_error = current_runner.detect_infra_error(
                    result.returncode,
                    result.output or "",
                    getattr(result, "stderr_tail", "") or "",
                )
                error_label = (
                    "other" if infra_error == InfraErrorType.NONE else infra_error.value
                )
                logger.warning(
                    f"LLM call failed ({error_label}) on agent '{current_agent_name}', "
                    f"attempting agent rotation..."
                )
                attempt_errors.append(
                    f"attempt {internal_attempt + 1}: agent '{current_agent_name}' failed "
                    f"(infra_error={error_label}, exit={result.returncode}, "
                    f"cmd={result.cmd_used})"
                )
                if self._rotate_agent():
                    # Rotation succeeded — next iteration uses the new agent.
                    # This consumes one of the max_retries attempt slots.
                    # A rotated-to agent has no session of its own for this
                    # step, so every attempt after a rotation rebuilds.
                    native_resume_disabled = True
                    time.sleep(self.retry_delay)
                    continue
                # Rotation exhausted — fall through; remaining attempts run on
                # the last agent (existing tail-on-last-agent behavior).

                logger.warning(
                    f"LLM call failed: {attempt_errors[-1]}, "
                    f"internal attempt {internal_attempt + 1}/{attempt_budget}"
                )

            except Exception as e:
                if not active_usage_recorded:
                    if active_tracker is not None:
                        failed_usage = active_tracker.usage_record
                    else:
                        failed_usage = parse_usage_record(
                            active_output,
                            call_id=active_call_id,
                            attempt=internal_attempt,
                            agent_name=attempt_agent_name,
                            runner_type=attempt_runner_type,
                            provider=effective_provider,
                            provider_session_id=attempt_session_id(),
                            configured_model=configured_model,
                            runner_startup_model=startup_model,
                        )
                    add_call_usage(failed_usage)
                    active_usage_recorded = True
                    if not active_response_recorded:
                        self._record_response(
                            active_output,
                            self.external_attempt,
                            agent_name=attempt_agent_name,
                            usage_record=failed_usage,
                            provider_session_id=attempt_session_id(),
                            session_cwd=str(attempt_cwd),
                            resume_strategy=attempt_strategy,
                            runner_type=attempt_runner_type,
                            kind=record_kind,
                        )
                        active_response_recorded = True
                # A native resume can fail by RAISING as readily as by exiting
                # non-zero: an installed CLI that rejects the resume argv, a
                # runner whose build_resume_call_args cannot express the
                # request, a subprocess that never starts. All of those are
                # reasons to rebuild context — never reasons to spend every
                # remaining slot re-issuing the same broken resume, which is
                # what left the sequence ending in a hard LLMCallError without
                # a single rebuild attempt.
                #
                # ``resume_produced_result`` bounds the classification to the
                # launch/run itself: once the resumed turn has answered and been
                # recorded, anything raised downstream is an ordinary attempt
                # failure and must neither mark the session rejected nor buy an
                # extra slot.
                if resume_plan is not None and not resume_produced_result:
                    logger.warning(
                        "Native resume of session %s raised on agent '%s'; "
                        "falling back to rebuilt context",
                        resume_plan["session_id"], attempt_agent_name,
                    )
                    attempt_errors.append(
                        f"attempt {internal_attempt + 1}: native resume of "
                        f"session {resume_plan['session_id']} raised "
                        f"{type(e).__name__}: {e} — falling back to rebuild"
                    )
                    native_resume_disabled = True
                    resume_fallback_slots += 1
                    self._fall_back_from_resume()
                    continue
                attempt_errors.append(
                    f"attempt {internal_attempt + 1}: agent '{attempt_agent_name}' "
                    f"raised {type(e).__name__}: {e}"
                )
                logger.warning(f"LLM call exception: {e}, internal attempt {internal_attempt + 1}/{attempt_budget}")

            if internal_attempt < attempt_budget - 1:
                time.sleep(self.retry_delay)

        reasons = "\n".join(attempt_errors) if attempt_errors else "no failure reason recorded"
        raise LLMCallError(
            f"LLM call failed after {internal_attempt} attempts:\n{reasons}"
        )

    @staticmethod
    def _extract_text_from_ndjson(output: str) -> Optional[str]:
        """Extract text content from NDJSON stream output.

        Parses the raw NDJSON output from Claude CLI's stream-json format
        and extracts text from assistant messages. Falls back to None if
        no text content can be extracted (caller should use raw output).

        Args:
            output: Raw NDJSON output string from Claude CLI.

        Returns:
            Extracted text content, or None if no text could be extracted.
        """
        lines = output.strip().split('\n')
        text_parts = []

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Strip '=== Command: ... ===' prefix line
            if line.startswith('=== Command:') and line.endswith('==='):
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not isinstance(data, dict):
                continue

            if data.get('type') == 'assistant':
                message = data.get('message', {})
                content = message.get('content', [])
                for item in content:
                    if isinstance(item, dict) and item.get('type') == 'text':
                        text = item.get('text', '')
                        if text:
                            text_parts.append(text)

        if not text_parts:
            return None

        return ''.join(text_parts)

    @staticmethod
    def _extract_result_text(raw_ndjson: str) -> Optional[str]:
        """Extract the result text from a type: "result" NDJSON message.

        This is the LLM's complete final output text — the synthesized
        conclusion after all tool calls and reasoning.

        Args:
            raw_ndjson: Raw NDJSON output string from Claude CLI.

        Returns:
            The result text, or None if no result message found.
        """
        if not raw_ndjson:
            return None
        for line in raw_ndjson.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get('type') == 'result':
                result_text = data.get('result')
                if result_text:
                    return result_text
        return None

    @staticmethod
    def _contains_valid_json(output: str) -> bool:
        """Check if the output contains valid JSON in the assistant's text content."""
        from .utils.json_parser import parse_json_response
        result = parse_json_response(output)
        return result is not None

    @classmethod
    def _create_json_retry_directive(cls, bad_output: str) -> str:
        """The JSON-fix request on its own, for a continuation turn.

        Same content as :meth:`_create_json_retry_prompt` minus the original
        prompt: a continuation is appended to a conversation that already holds
        it, so re-sending it would be noise the model has to reconcile.
        """
        text_content = cls._extract_assistant_text(bad_output)
        return (
            "IMPORTANT: Your previous response was not in the required JSON "
            "format. You responded with:\n---\n"
            f"{text_content[:1500]}\n---\n\n"
            "Please respond ONLY with valid JSON as specified in the "
            "instructions for this step. Do not include any explanatory text "
            "before or after the JSON."
        )

    @staticmethod
    def _extract_assistant_text(bad_output: str) -> str:
        """Concatenate the assistant text of a stream-json output."""
        text_content = ""
        for line in (bad_output or "").strip().split("\n"):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("type") == "assistant":
                message = data.get("message", {})
                for item in message.get("content", []) or []:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text = item.get("text", "")
                        if text:
                            text_content += text
        return text_content

    @staticmethod
    def _create_json_retry_prompt(original_prompt: str, bad_output: str) -> str:
        """Create a prompt asking LLM to return JSON format."""
        # Extract what the LLM said (from assistant messages)
        text_content = ""
        for line in bad_output.strip().split('\n'):
            try:
                data = json.loads(line)
                if isinstance(data, dict) and data.get('type') == 'assistant':
                    message = data.get('message', {})
                    content = message.get('content', [])
                    for item in content:
                        if isinstance(item, dict) and item.get('type') == 'text':
                            text = item.get('text', '')
                            if text:
                                text_content += text
            except json.JSONDecodeError:
                continue

        retry_prompt = f"""{original_prompt}

IMPORTANT: Your previous response was not in the required JSON format. You responded with:
---
{text_content[:1500]}
---

Please respond ONLY with valid JSON as specified in the instructions above. Do not include any explanatory text before or after the JSON."""

        return retry_prompt
