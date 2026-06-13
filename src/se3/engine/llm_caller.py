"""LLM caller for step execution.

Handles subprocess calls to Claude CLI with retry and fallback logic.
Manages agent selection and rotation on infrastructure errors.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from ..agent_runner import AgentRunner, InfraErrorType
from ..claude_runner import ClaudeCodeRunner, ClaudeRunner
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

# Module-level extra prompt state for Ctrl+C injection (transient, consumed after one use)
_extra_prompt: Optional[str] = None
# Persistent extra prompt state for loop context injection (survives across LLM calls)
_persistent_extra_prompt: Optional[str] = None
# Lock protecting _extra_prompt and _persistent_extra_prompt for thread safety
_extra_prompt_lock = threading.Lock()


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
            global _extra_prompt
            _extra_prompt = prompt


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


def clear_persistent_extra_prompt() -> None:
    """Clear only the persistent extra prompt (for cleanup between loop iterations)."""
    with _extra_prompt_lock:
        global _persistent_extra_prompt
        _persistent_extra_prompt = None


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


from .tool_formatters import (
    build_tool_detail_payload,
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
        agent_name: Optional[str] = None,
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
        # Actual model name (e.g. "claude-opus-4-8"), best-effort parsed from
        # the stream's init/system metadata as lines arrive. Stays None until a
        # model is seen; once set, subsequent progress lines carry it so the
        # frontend upgrades the bubble label from "agent" to "agent · model".
        self._model_name: Optional[str] = None
        # Pending coalesced text/thinking awaiting a flush.
        self._progress_text_buf: List[str] = []
        self._progress_text_len = 0
        # Token / cost usage captured from the type:"result" message. Stays an
        # empty tally until a result line carrying usage is seen; exposed via
        # the read-only ``usage`` property for the caller to fold into the
        # current step accumulator.
        self._usage = UsageTotals()

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
        the frontend's single-chip state machine: an in-flight tool chip
        carries ``tool_use_id`` with ``tool_detail=None``; the terminal chip
        carries the same id plus ``is_error`` and the structured detail
        payload. They are forwarded to ``record_stream_progress`` only when
        non-default so narrative-text progress lines stay byte-identical to
        the legacy schema.
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
            print(f"  {self.stream_prefix}[llm-stream] ❌ Tool error: {error_preview}...")
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

            # Best-effort parse the actual model name from this line's
            # init/system metadata. The first match is cached so subsequent
            # progress lines carry "agent · model"; a parse miss leaves the
            # cache untouched and never disturbs the stream.
            if self._model_name is None:
                from .chat_history import extract_model_name_from_obj

                model = extract_model_name_from_obj(data)
                if model:
                    self._model_name = model

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
                                # tool_detail=None marks the in-flight state for
                                # the frontend chip state machine; the matching
                                # terminal emit on tool_result fills it in.
                                tool_detail=None,
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
                print(f"  {self.stream_prefix}[llm-stream] ❌ Error: {truncate_preview(str(error_msg))}")
                self._flush_progress_text()
                self._emit_progress(f"[Tool error: {truncate_preview(str(error_msg))}]", None)
                self._last_ended_with_newline = True

            elif msg_type == 'result':
                # The terminal result line carries the call's token usage and
                # cost. Capture them silently — this does NOT touch stdout or the
                # stream_progress channel, so the human-readable terminal stream
                # and the web/jsonl bytes are unchanged.
                self._capture_usage(data)

        except json.JSONDecodeError:
            # Not valid JSON, might be a partial line
            pass

    def _capture_usage(self, data: Dict[str, Any]) -> None:
        """Capture token usage + cost from a type:"result" NDJSON message.

        Reads the four token counts from ``message.usage`` (nested form) or a
        top-level ``usage`` (flat form), plus the top-level ``total_cost_usd``.
        Missing fields count as 0 and any structural surprise is swallowed, so a
        malformed or partial result line never disrupts the stream.
        """
        try:
            usage_obj = None
            message = data.get("message")
            if isinstance(message, dict):
                usage_obj = message.get("usage")
            if not isinstance(usage_obj, dict):
                top_usage = data.get("usage")
                if isinstance(top_usage, dict):
                    usage_obj = top_usage
            captured = UsageTotals.from_dict(usage_obj if isinstance(usage_obj, dict) else None)
            # total_cost_usd lives at the top level of the result message, not
            # inside usage; from_dict on usage_obj leaves it at 0.0, so fill it
            # here.
            if "total_cost_usd" in data:
                captured.total_cost_usd = UsageTotals.from_dict(
                    {"total_cost_usd": data.get("total_cost_usd")}
                ).total_cost_usd
            self._usage = captured
        except Exception:  # pragma: no cover - defensive; never break the stream
            logger.debug("Failed to capture usage from result message", exc_info=True)

    @property
    def usage(self) -> UsageTotals:
        """Token / cost usage captured from this stream's result message.

        An empty :class:`UsageTotals` until a ``type:"result"`` line carrying
        usage has been processed.
        """
        return self._usage

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
        print(f"  {self.stream_prefix}[llm-stream] ✓ Stream complete: {self.message_count} messages, "
              f"{len(self.tool_calls)} tool calls, {self.total_text_len} chars "
              f"({duration:.1f}s)")
        # Clean up caches to prevent memory leaks on stream interruption
        self._tool_use_id_to_input.clear()
        self._tool_use_id_to_old_content.clear()
        self._tool_use_id_to_name.clear()


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
    ):
        self.project_root = Path(project_root) if project_root else Path.cwd()
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
            from se3.codex_runner import CodexRunner
            return CodexRunner(
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

    def _rotate_agent(self) -> bool:
        """Rotate to the next agent in the list.

        Returns:
            True if rotation succeeded, False if all agents are exhausted.
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
            **kwargs: Ignored (accepts model, max_tokens, temperature
                      for forward-compatibility but they don't apply
                      to claude -p subprocess calls)

        Returns:
            LLM output text (JSON if json_mode is not "off")

        Raises:
            LLMCallError: If all retries exhausted or extraction fails
        """
        # Resolve JSON mode from various parameter combinations
        mode = self._resolve_json_mode(json_mode, require_json, two_phase_json)

        # Inject extra prompts if set (persistent for loop context, transient for Ctrl+C)
        with _extra_prompt_lock:
            global _extra_prompt
            injected_parts = []
            if _persistent_extra_prompt:
                injected_parts.append(_persistent_extra_prompt)
                logger.info(f"Injected persistent extra prompt: {_persistent_extra_prompt[:80]}")
            if _extra_prompt:
                injected_parts.append(_extra_prompt)
                logger.info(f"Injected transient extra prompt: {_extra_prompt[:80]}")
                _extra_prompt = None  # Consume transient after use
        if injected_parts:
            prompt = f"{prompt}\n\n[Additional user instruction]: {chr(10).join(injected_parts)}"

        # Inject read-only constraint for read-only steps
        from .context_builder import get_read_only_injection
        read_only_constraint = get_read_only_injection(self.step_type)
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
            )
        elif mode == "extract":
            return self._call_extract(
                prompt=prompt,
                timeout=timeout,
                context_files=context_files,
                on_output=on_output,
                json_schema_hint=json_schema_hint,
                required_keys=required_keys,
            )
        elif mode == "strict":
            return self._call_strict(
                prompt=prompt,
                timeout=timeout,
                context_files=context_files,
                on_output=on_output,
            )
        else:  # mode == "off"
            return self._call_with_retry(
                prompt=prompt,
                timeout=timeout,
                context_files=context_files,
                on_output=on_output,
                require_json=False,
                json_retry_count=0,
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
        )

    def _call_extract(
        self,
        prompt: str,
        timeout: Optional[int],
        context_files: Optional[List[Path]],
        on_output: Optional[Callable[[str], None]],
        json_schema_hint: Optional[str],
        required_keys: Optional[List[str]] = None,
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
        )

        # Fast path: lenient parse (dict with required_keys, or list when no required_keys)
        fast = self._lenient_parse_extract(output, required_keys)
        if fast is not None:
            return json.dumps(fast, ensure_ascii=False, indent=2)

        # Fallback: extract JSON via second-phase LLM call
        print(f"  {self.stream_prefix}[llm-caller] 🔍 Extracting JSON from output (extract mode)...")

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

        print(f"  {self.stream_prefix}[llm-caller] ✅ JSON extraction complete")
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
                print(f"  {self.stream_prefix}[llm-caller] ⏩ Phase 1 skipped (cached from previous attempt)")
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
                print(f"  {self.stream_prefix}[llm-caller] ✅ Phase 1 output contained valid JSON, phase 2 skipped")
                # Step fully done — delete cache
                if cache_path and cache_path.exists():
                    try:
                        cache_path.unlink()
                    except OSError as e:
                        logger.warning(f"Failed to delete Phase 1 cache: {e}")
                return json.dumps(result, ensure_ascii=False, indent=2)
            else:
                logger.info("Two-phase: phase 1 JSON missing required keys %s, falling back to phase 2", required_keys)
                print(f"  {self.stream_prefix}[llm-caller] ⚠️  Phase 1 JSON missing required keys, falling back to phase 2")

        # Phase 2: Extract JSON via LLM
        print(f"  {self.stream_prefix}[llm-caller] 🔍 Phase 2: Extracting JSON from output...")

        from .json_extractor import JSONExtractor

        extractor = JSONExtractor(
            project_root=self.project_root,
            timeout=300,  # 5 minutes for large outputs
        )

        result = extractor.extract(
            raw_output=phase1_output,
            schema_hint=json_schema_hint,
            required_keys=required_keys,
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

        print(f"  {self.stream_prefix}[llm-caller] ✅ JSON extraction complete")
        return json_str

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

    def _record_prompt(self, prompt: str, attempt: int, agent_name: Optional[str] = None) -> None:
        """Record a prompt to chat history if flow context is available.

        ``agent_name`` (default None) records the configuration name of the
        agent that will handle this prompt. Failures inside record_prompt are
        caught and debug-logged so metadata recording never disrupts the LLM
        call.
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
            )
        except Exception as e:
            logger.debug(f"Failed to record prompt to history: {e}")

    def _record_response(self, raw_ndjson: str, attempt: int, agent_name: Optional[str] = None) -> None:
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
            )
        except Exception as e:
            logger.warning(f"Failed to get retry context (falling back to original prompt): {e}")
            return None

    def _call_with_retry(
        self,
        prompt: str,
        timeout: int,
        context_files: Optional[List[Path]],
        on_output: Optional[Callable[[str], None]],
        require_json: bool,
        json_retry_count: int,
        max_json_retries: int = 2,
    ) -> str:
        """Internal method to call LLM with retry and agent rotation logic.

        On infrastructure errors (usage limit, timeout, hang), rotates to the
        next agent and retries.  On task-level failures, retries with the same
        agent up to ``max_retries`` times before raising.
        """
        original_prompt = prompt

        # Reset touched-files tracking for this call
        self._last_touched_files = set()

        env = dict(os.environ)
        env.pop("CLAUDECODE", None)

        start_time = time.time()
        last_error = ""

        for internal_attempt in range(self.max_retries):
            is_retry = self.external_attempt > 0 or internal_attempt > 0

            # Snapshot the current agent name at the start of this attempt.
            # This captures the agent BEFORE any rotation that might occur
            # during this attempt's failure path, so both prompt and response
            # records for this attempt carry the same agent attribution.
            attempt_agent_name = self._agents[self._current_agent_index].get("name", "?")

            # On retry (either external or internal), inject previous conversation context
            if is_retry:
                retry_context = self._get_retry_context()
                if retry_context:
                    if self.retry_mode == "continue":
                        # In continue mode, the original prompt is already in the history.
                        # Append a short continuation instruction instead of re-prepending the full prompt.
                        effective_prompt = (
                            f"{retry_context}\n"
                            "Continue the task from where you left off based on the conversation history above. "
                            "Do NOT repeat work already completed."
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
            if is_retry:
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

            # Record the original prompt (NOT effective_prompt) to chat history.
            # effective_prompt on retries contains the retry-context block (marker..separator).
            # If we recorded that, the next retry's format_history_for_retry would read it back
            # as a user message and re-embed it inside a fresh retry-context, producing
            # second-order recursive bloat across attempts. Recording original_prompt keeps
            # the persistent record clean — the retry-context is rebuilt from history each call.
            self._record_prompt(original_prompt, self.external_attempt, agent_name=attempt_agent_name)

            try:
                current_runner = self._get_current_runner()
                current_agent_name = self._agents[self._current_agent_index].get("name", "?")

                # Delegate CLI argument construction to the runner.  Each
                # runner translates the caller's intent (prompt, read-only
                # flag, context files) into its own agent-specific CLI flags.
                from .context_builder import is_step_read_only
                args = current_runner.build_call_args(
                    prompt=effective_prompt,
                    read_only=is_step_read_only(self.step_type),
                    context_files=context_files,
                )
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

                if on_output:
                    result = current_runner.run_with_monitor(
                        args=args,
                        wall_timeout=None,  # No wall time limit, only inactivity timeout
                        inactivity_timeout=1800,  # 30 minutes
                        cwd=self.project_root,
                        env=env,
                        on_output=on_output,
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
                        stream_prefix=self.stream_prefix,
                        project_root=self.project_root,
                        flow_id=self.flow_id,
                        step_id=self.step_id,
                        step_type=self.step_type,
                        attempt=self.external_attempt,
                        # Same agent name used for this attempt's prompt/response
                        # records, so the streamed progress lines, the prompt,
                        # and the response all agree on the agent that actually
                        # ran — and a rotation/retry's fresh tracker carries the
                        # new agent rather than the stale one.
                        agent_name=attempt_agent_name,
                    )

                    def on_stream_output(line: str) -> None:
                        stream_tracker.process_line(line)

                    result = current_runner.run_with_monitor(
                        args=args,
                        wall_timeout=None,  # No wall time limit, only inactivity timeout
                        inactivity_timeout=1800,  # 30 minutes
                        cwd=self.project_root,
                        env=env,
                        on_output=on_stream_output,
                        on_confirm=on_confirm,
                    )

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
                    add_call_usage(stream_tracker.usage)

                # Record the response (whether success, failure, or interrupted)
                self._record_response(result.output or "", self.external_attempt, agent_name=attempt_agent_name)

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
                            print(f"  {self.stream_prefix}[llm-caller] ⚠️  Response is not valid JSON, requesting JSON format (retry {json_retry_count + 1}/{max_json_retries})")
                            json_prompt = self._create_json_retry_prompt(prompt, result.output)
                            # Record the JSON retry prompt too (use a distinct attempt number for JSON retries)
                            json_attempt = self.external_attempt * 100 + json_retry_count  # Distinguish JSON retries
                            self._record_prompt(json_prompt, json_attempt, agent_name=attempt_agent_name)
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
                            )

                    duration_s = time.time() - start_time
                    logger.debug(f"LLM call succeeded in {int(duration_s * 1000)}ms")
                    return result.output

                # --- Failure path: always attempt agent rotation ---
                # detect_infra_error is retained for diagnostic labeling only;
                # USAGE_LIMIT / TIMEOUT / OTHER all trigger rotation identically.
                # Pass stderr_tail when available (CodexRunner populates it;
                # ClaudeCodeRunner has no such field so getattr defaults to "").
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
                if self._rotate_agent():
                    # Rotation succeeded — next iteration uses the new agent.
                    # This consumes one of the max_retries attempt slots.
                    time.sleep(self.retry_delay)
                    continue
                # Rotation exhausted — fall through; remaining attempts run on
                # the last agent (existing tail-on-last-agent behavior).

                last_error = f"Command '{result.cmd_used}' failed with exit code {result.returncode}"
                logger.warning(f"LLM call failed: {last_error}, internal attempt {internal_attempt + 1}/{self.max_retries}")

            except Exception as e:
                last_error = str(e)
                logger.warning(f"LLM call exception: {last_error}, internal attempt {internal_attempt + 1}/{self.max_retries}")

            if internal_attempt < self.max_retries - 1:
                time.sleep(self.retry_delay)

        raise LLMCallError(f"LLM call failed after {self.max_retries} attempts: {last_error}")

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
