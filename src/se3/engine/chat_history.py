"""Chat history system for LLM call tracking.

Records prompts and responses for each flow step, enables retry context
injection, and provides human-readable browsing of conversation history.

Storage format: se3/history/{flow_id}/{step_id}.jsonl
Each line is a JSON-serialized ChatMessage.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

from .retry_context import (
    POST_DEDUP_SAFETY_LIMIT,
    RETRY_HISTORY_MARKER,
    RETRY_HISTORY_SEPARATOR,
)
from .tool_formatters import (
    format_tool_result_preview,
    format_tool_use_preview,
    truncate_preview,
)


# Default project root for history storage
_SE3_DIR = "se3"

_HISTORY_DIR = "history"


@dataclass
class ChatMessage:
    """A single message in a chat session."""

    role: str  # "user" | "assistant"
    content: str  # Parsed text content
    raw_json: list[dict]  # Parsed JSON messages from NDJSON stream (assistant only)
    timestamp: str  # ISO format
    step_type: str  # e.g. "analyze", "plan"
    attempt: int  # 0-based attempt number
    # Fix-loop iteration the message belongs to. Distinct from ``attempt``,
    # which counts LLMCaller-internal retries within one logical call. A
    # single step_id (e.g. an implement step reused across fix iterations)
    # can collect messages from multiple iterations; retry-context
    # construction must filter to the current iteration to avoid
    # cross-iteration bleed-through.
    #
    # Backward-compat: messages predating this field deserialize with
    # ``fix_iteration=0``. ``format_history_for_retry`` treats 0 as a
    # wildcard so legacy jsonl is not filtered out after upgrade.
    fix_iteration: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ChatMessage:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


@dataclass
class ChatSession:
    """A complete chat session for one flow step."""

    flow_id: str
    step_id: str
    step_type: str
    messages: List[ChatMessage] = field(default_factory=list)


def _history_dir(project_root: Path, flow_id: str) -> Path:
    """Get the history directory for a flow."""
    return project_root / _SE3_DIR / _HISTORY_DIR / flow_id


def _history_file(project_root: Path, flow_id: str, step_id: str) -> Path:
    """Get the history file for a step."""
    return _history_dir(project_root, flow_id) / f"{step_id}.jsonl"


def record_prompt(
    project_root: Path,
    flow_id: str,
    step_id: str,
    step_type: str,
    prompt: str,
    attempt: int,
    fix_iteration: int = 0,
) -> None:
    """Record a user prompt sent to the LLM.

    ``fix_iteration`` (default 0) tags the message with the current
    fix-loop iteration so retry-context construction can filter messages
    by iteration boundary. Default 0 keeps the API backward-compatible
    for non-fix-loop callers.
    """
    msg = ChatMessage(
        role="user",
        content=prompt,
        raw_json=[],
        timestamp=datetime.now().isoformat(),
        step_type=step_type,
        attempt=attempt,
        fix_iteration=fix_iteration,
    )
    _append_message(project_root, flow_id, step_id, msg)


def record_response(
    project_root: Path,
    flow_id: str,
    step_id: str,
    step_type: str,
    raw_ndjson: str,
    attempt: int,
    fix_iteration: int = 0,
) -> None:
    """Record an LLM response (raw NDJSON output).

    See :func:`record_prompt` for the ``fix_iteration`` semantics.
    """
    text = extract_assistant_text(raw_ndjson)
    # Parse NDJSON string into list of dicts for storage
    raw_json: list[dict] = []
    if raw_ndjson and raw_ndjson.strip():
        for line in raw_ndjson.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("==="):
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    raw_json.append(parsed)
            except (json.JSONDecodeError, TypeError):
                continue
    msg = ChatMessage(
        role="assistant",
        content=text,
        raw_json=raw_json,
        timestamp=datetime.now().isoformat(),
        step_type=step_type,
        attempt=attempt,
        fix_iteration=fix_iteration,
    )
    _append_message(project_root, flow_id, step_id, msg)


def record_step_event(
    project_root: Path,
    flow_id: str,
    step_id: str,
    step_type: str,
    event_type: str,
    step_dict: Dict[str, Any],
    timestamp: Optional[float] = None,
) -> None:
    """Record a step-lifecycle event (``step_completed`` / ``step_failed``).

    Writes a single JSON line into ``se3/history/{flow_id}/{step_id}.jsonl``
    alongside the LLM user/assistant messages. The line is intentionally NOT
    a :class:`ChatMessage` — it carries the engine's structured step output
    in the shape the web frontend's ``normalizeRecord`` expects, so the same
    content the CLI's ``step_renderers`` Panel shows surfaces as a report
    card in the running-flow console.

    The daemon's history reader (``DaemonHistoryReader.read_flow``) reads any
    JSON dict on each line and forwards it as-is to the server, so this line
    rides the existing ``history_data`` push channel without protocol
    changes. :func:`get_step_history` skips non-ChatMessage lines gracefully.
    """
    record = {
        "type": event_type,
        "step_id": step_id,
        "step_type": step_type,
        "timestamp": (
            datetime.fromtimestamp(timestamp).isoformat()
            if timestamp is not None
            else datetime.now().isoformat()
        ),
        "data": {"step": step_dict},
    }
    path = _history_file(project_root, flow_id, step_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        logger.warning("Failed to record step event for %s: %s", step_id, exc)


def record_stream_progress(
    project_root: Path,
    flow_id: str,
    step_id: str,
    step_type: str,
    content: str,
    raw_obj: Any,
    attempt: int,
    timestamp: Optional[str] = None,
) -> None:
    """Append a single in-progress (partial) stream line to the step jsonl.

    Unlike :func:`record_response`, which writes the *final* result once a turn
    completes, this writes process content **incrementally, before the final
    result lands** — each semantic stream event (assistant text/thinking block,
    tool_use, tool_result) is flushed as its own line as it streams. The
    daemon's incremental reader (``DaemonHistoryReader.read_flow``) picks the
    new line up on its next cursor advance and forwards it over the existing
    ``history_data`` push channel, so the web console can render the running
    step's output line by line instead of staring at a blank step until the
    final JSON arrives.

    The line is a self-contained dict (not a :class:`ChatMessage`) carrying
    ``type='stream_progress'`` and ``partial=True`` so the frontend can group
    it under its turn and fold it away once the final assistant result for the
    same ``(step_id, attempt)`` arrives. ``get_step_history`` (and therefore
    ``format_history_for_retry``) skips ``stream_progress`` lines so CLI
    history rendering and retry-context construction never ingest the
    intermediate process.

    Follows :func:`record_step_event`'s write semantics — ``mkdir`` + a single
    whole-line ``write`` (so a half-written final line cannot corrupt earlier
    lines) wrapped in an ``OSError`` guard so a write failure never breaks the
    in-flight LLM call.
    """
    record = {
        "type": "stream_progress",
        "role": "assistant",
        "step_type": step_type,
        "content": content,
        "raw_json": [raw_obj] if raw_obj is not None else [],
        "timestamp": timestamp or datetime.now().isoformat(),
        "attempt": attempt,
        "partial": True,
    }
    path = _history_file(project_root, flow_id, step_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        logger.warning("Failed to record stream progress for %s: %s", step_id, exc)


def get_step_history(
    project_root: Path, flow_id: str, step_id: str
) -> Optional[ChatSession]:
    """Get the complete chat session for a step."""
    path = _history_file(project_root, flow_id, step_id)
    if not path.exists():
        return None

    messages = []
    step_type = ""
    for line in path.read_text(encoding="utf-8").strip().split("\n"):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            # Step-lifecycle event records (written by HistorySink) live in the
            # same jsonl but are not ChatMessages — skip them here so CLI
            # history rendering only sees the user/assistant turns.
            if isinstance(data, dict) and data.get("type") in (
                "step_completed",
                "step_failed",
            ) and "role" not in data:
                continue
            # Stream-progress records (written by record_stream_progress) carry
            # a ``role`` field and would otherwise deserialize as a ChatMessage,
            # so they must be skipped explicitly. They are the *intermediate*
            # process for a turn whose final result is recorded separately by
            # record_response; CLI history and retry-context construction
            # (format_history_for_retry, which reads through get_step_history)
            # MUST NOT ingest them.
            if isinstance(data, dict) and data.get("type") == "stream_progress":
                continue
            msg = ChatMessage.from_dict(data)
            messages.append(msg)
            if not step_type:
                step_type = msg.step_type
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"Skipping malformed history line: {e}")
            continue

    if not messages:
        return None

    return ChatSession(
        flow_id=flow_id,
        step_id=step_id,
        step_type=step_type,
        messages=messages,
    )


def get_flow_history(project_root: Path, flow_id: str) -> List[ChatSession]:
    """Get all chat sessions for a flow."""
    flow_dir = _history_dir(project_root, flow_id)
    if not flow_dir.exists():
        return []

    sessions = []
    for path in sorted(flow_dir.glob("*.jsonl")):
        step_id = path.stem
        session = get_step_history(project_root, flow_id, step_id)
        if session:
            sessions.append(session)

    return sessions


def list_flows(project_root: Path) -> List[str]:
    """List all flow IDs that have history."""
    history_root = project_root / _SE3_DIR / _HISTORY_DIR
    if not history_root.exists():
        return []
    return sorted(
        d.name for d in history_root.iterdir() if d.is_dir()
    )


def split_implement_session_by_iterations(
    session: ChatSession, test_timestamps: List[str]
) -> List[ChatSession]:
    """Split an implement ChatSession into virtual per-iteration sessions.

    Fix loops re-use the same implement Step (status reset, not a new step),
    so multi-iteration implement prompts accumulate in a single jsonl file.
    For display purposes, partition the messages using test session
    timestamps as fences: messages before the first test belong to iter1,
    messages between test[i-1] and test[i] belong to iter{i+1}, etc.

    Args:
        session: The implement ChatSession to split.
        test_timestamps: ISO-formatted timestamps of test sessions in the
            same flow. Order does not matter; internally sorted.

    Returns:
        A list of virtual ChatSessions. Each virtual session's step_id
        equals the original step_id plus a ``-iter{N}`` suffix (N starts
        at 1). Returns ``[session]`` unchanged when only one iteration is
        present (single iteration means the original session already
        represents iter1 exactly). Returns ``[]`` when the input session
        has no messages.
    """
    if not session.messages:
        return []

    sorted_fences = sorted(t for t in test_timestamps if t)

    # Assign each message to an iteration index (1-based): count of test
    # fences at or before the message timestamp, plus one.
    import bisect

    iteration_buckets: Dict[int, List[ChatMessage]] = {}
    for msg in session.messages:
        idx = bisect.bisect_right(sorted_fences, msg.timestamp) + 1
        iteration_buckets.setdefault(idx, []).append(msg)

    if len(iteration_buckets) <= 1:
        return [session]

    # Renumber labels 1..N in bucket-order so gaps caused by empty fence
    # intervals (e.g. a fix iteration whose implement re-entry produced no
    # new assistant messages in that window) do not surface as
    # non-consecutive -iter labels.
    virtual_sessions: List[ChatSession] = []
    for display_idx, bucket_idx in enumerate(sorted(iteration_buckets), start=1):
        virtual_sessions.append(
            ChatSession(
                flow_id=session.flow_id,
                step_id=f"{session.step_id}-iter{display_idx}",
                step_type=session.step_type,
                messages=iteration_buckets[bucket_idx],
            )
        )
    return virtual_sessions


def interleave_sessions_for_display(
    sessions: List[ChatSession],
) -> List[ChatSession]:
    """Reorder sessions so virtual-split implement iterations interleave
    with test/self_check by timestamp.

    Identifies implement sessions (``step_type == "implement"``), splits
    each via :func:`split_implement_session_by_iterations` using the
    first-message timestamps of test sessions as fences, then stable-sorts
    the resulting sessions by each session's first message timestamp
    (tiebreaker: ``step_id``). Non-implement sessions pass through
    unchanged.

    Args:
        sessions: All ChatSessions for a single flow (as returned by
            :func:`get_flow_history`).

    Returns:
        Sessions reordered so that ``implement-iter1 → test-1 →
        self_check-1 → implement-iter2 → ...`` appears in chronological
        order. Empty implement sessions are dropped (they carry no
        useful display content).
    """
    test_timestamps = [
        s.messages[0].timestamp
        for s in sessions
        if s.step_type == "test" and s.messages
    ]

    expanded: List[ChatSession] = []
    for session in sessions:
        if session.step_type == "implement":
            expanded.extend(
                split_implement_session_by_iterations(session, test_timestamps)
            )
        elif session.messages:
            # Drop empty non-implement sessions uniformly. Disk loaders
            # already skip empty jsonl files, but in-memory callers (tests,
            # future code paths) can construct empty sessions whose
            # fallback sort key would place them at the very beginning of
            # the timeline.
            expanded.append(session)

    def sort_key(s: ChatSession) -> tuple:
        # After the filter above, non-implement sessions always have
        # messages. Implement-split sessions may rarely produce an empty
        # bucket; guard with a trailing sentinel so they sink rather than
        # float to the top.
        first_ts = s.messages[0].timestamp if s.messages else "\uffff"
        return (first_ts, s.step_id)

    return sorted(expanded, key=sort_key)


@dataclass
class ConversationMessage:
    """A single message in a conversation for LLM context."""

    role: str  # "user" | "assistant"
    content: str
    tool_calls: Optional[List[dict]] = None  # For assistant messages with tool calls
    tool_results: Optional[List[dict]] = None  # For user messages with tool results


def extract_conversation_from_ndjson(raw_ndjson: Union[str, list[dict]]) -> List[ConversationMessage]:
    """Extract structured conversation from NDJSON output.

    Parses the stream-json format and reconstructs the conversation flow
    including assistant messages, tool calls, and tool results.

    Args:
        raw_ndjson: The raw NDJSON output from Claude CLI (str) or parsed list[dict]

    Returns:
        List of ConversationMessage objects representing the conversation
    """
    if not raw_ndjson:
        return []

    # Handle list[dict] input (new format) vs string input
    if isinstance(raw_ndjson, list):
        parsed_items = raw_ndjson
    else:
        # Original string format - parse each line
        parsed_items = None
        json_lines = raw_ndjson.strip().split("\n")

    messages: List[ConversationMessage] = []
    pending_tool_calls: List[dict] = []
    pending_tool_results: List[dict] = []

    # Iterate over either pre-parsed items or string lines
    items = parsed_items if parsed_items is not None else json_lines

    for item in items:
        try:
            if isinstance(item, dict):
                data = item
            else:
                line = item.strip()
                if not line or line.startswith("==="):
                    continue
                data = json.loads(line)

            if not isinstance(data, dict):
                continue

            msg_type = data.get("type", "")

            if msg_type == "assistant":
                message = data.get("message", {})
                content = message.get("content", [])

                text_parts = []
                tool_calls = []

                for item in content:
                    if not isinstance(item, dict):
                        continue

                    if item.get("type") == "text":
                        text = item.get("text", "")
                        if text:
                            text_parts.append(text)
                    elif item.get("type") == "tool_use":
                        tool_calls.append({
                            "id": item.get("id", ""),
                            "name": item.get("name", "unknown"),
                            "input": item.get("input", {}),
                        })

                # If we have pending tool results from previous turn, add them first
                if pending_tool_results:
                    messages.append(ConversationMessage(
                        role="user",
                        content="",
                        tool_results=pending_tool_results.copy()
                    ))
                    pending_tool_results = []

                # Add the assistant message
                assistant_content = "\n".join(text_parts)
                messages.append(ConversationMessage(
                    role="assistant",
                    content=assistant_content,
                    tool_calls=tool_calls if tool_calls else None
                ))
                pending_tool_calls = tool_calls

            elif msg_type == "tool_result":
                # Standalone tool_result message (not inside user message)
                result = data.get("result", {})
                tool_result = {
                    "tool_use_id": result.get("toolUseId", ""),
                    "content": result.get("content", ""),
                    "is_error": result.get("isError", False),
                }
                pending_tool_results.append(tool_result)

            elif msg_type == "user":
                # In Claude CLI protocol, 'user' messages often contain tool results
                message = data.get("message", {})
                content = message.get("content", [])

                # Extract tool results from this user message
                current_tool_results = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        current_tool_results.append({
                            "tool_use_id": item.get("tool_use_id", "") or item.get("toolUseId", ""),
                            "content": item.get("content", ""),
                            "is_error": item.get("is_error", False) or item.get("isError", False),
                        })

                # Add any pending tool results first (from previous standalone tool_result messages)
                if pending_tool_results:
                    current_tool_results = pending_tool_results + current_tool_results
                    pending_tool_results = []

                # Create user message immediately with tool results from this message
                if current_tool_results:
                    messages.append(ConversationMessage(
                        role="user",
                        content="",
                        tool_results=current_tool_results
                    ))

        except (json.JSONDecodeError, TypeError, AttributeError, KeyError) as e:
            logger.debug(f"Skipping malformed NDJSON entry: {e}")
            continue

    # Add any remaining tool results as a user message
    if pending_tool_results:
        messages.append(ConversationMessage(
            role="user",
            content="",
            tool_results=pending_tool_results
        ))

    return messages


def format_conversation_for_llm(messages: List[ConversationMessage]) -> str:
    """Format conversation messages for LLM context.

    Creates a text representation that preserves the structure of
    the conversation including tool calls and results.
    Merges consecutive assistant messages for cleaner output.

    Args:
        messages: List of ConversationMessage objects

    Returns:
        Formatted string suitable for LLM context
    """
    if not messages:
        return ""

    # Merge consecutive assistant messages
    merged_messages: List[ConversationMessage] = []
    current_assistant: Optional[ConversationMessage] = None

    for msg in messages:
        if msg.role == "assistant":
            if current_assistant is None:
                current_assistant = ConversationMessage(
                    role="assistant",
                    content=msg.content or "",
                    tool_calls=list(msg.tool_calls) if msg.tool_calls else None,
                )
            else:
                # Merge with previous assistant message
                if msg.content:
                    if current_assistant.content:
                        current_assistant.content += "\n" + msg.content
                    else:
                        current_assistant.content = msg.content
                if msg.tool_calls:
                    if current_assistant.tool_calls:
                        current_assistant.tool_calls.extend(msg.tool_calls)
                    else:
                        current_assistant.tool_calls = list(msg.tool_calls)
        else:
            # User message - flush current assistant if any
            if current_assistant is not None:
                merged_messages.append(current_assistant)
                current_assistant = None
            merged_messages.append(msg)

    # Don't forget the last assistant message
    if current_assistant is not None:
        merged_messages.append(current_assistant)

    parts = []

    for msg in merged_messages:
        if msg.role == "assistant":
            parts.append("[Assistant]")

            if msg.content:
                parts.append(msg.content)

            if msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_name = tc.get("name", "unknown")
                    tool_input = tc.get("input", {})
                    preview = format_tool_use_preview(tool_name, tool_input)
                    parts.append(f"[{preview}]")

        elif msg.role == "user":
            parts.append("[User]")

            if msg.content:
                parts.append(msg.content)

            if msg.tool_results:
                for tr in msg.tool_results:
                    content = str(tr["content"])
                    if len(content) > 2000:
                        if tr.get("is_error"):
                            content = "... [truncated]\n" + content[-2000:]
                        else:
                            content = content[:2000] + "\n... [truncated]"
                    status = " (error)" if tr.get("is_error") else ""
                    parts.append(f"[Tool Result{status}]: {content}")

        parts.append("")  # Empty line between messages

    return "\n".join(parts)


def format_history_for_retry(
    project_root: Path,
    flow_id: str,
    step_id: str,
    mode: str = "continue",
    current_fix_iteration: int = 0,
) -> Optional[str]:
    """Format previous conversation attempts for retry context injection.

    Extracts the full conversation from raw NDJSON to preserve tool calls
    and results structure, rather than using simplified text summaries.

    Args:
        project_root: Project root directory
        flow_id: Flow instance ID
        step_id: Step instance ID
        mode: 'continue' (default) to resume from breakpoint, or 'retry' to restart.
              User prompts are preserved verbatim; repeated content (e.g. spec text
              repeated across attempts) is eliminated by deduplicate_prompt_lines()
              in LLMCaller, and an overall post-dedup safety cap is enforced there
              as a defensive fallback.
              In 'continue' mode: assistant responses with tool calls are not
              truncated, and a continuation instruction is appended.
              In 'retry' mode: preserves original behavior.
        current_fix_iteration: The fix-loop iteration of the in-flight LLM
              call. Messages tagged with a different non-zero
              ``fix_iteration`` are excluded from the retry context to
              prevent cross-iteration bleed-through (the implement step
              re-uses the same step_id across fix iterations, so its
              chat_history accumulates messages from all prior iterations).
              Messages with ``fix_iteration == 0`` are always included as a
              wildcard so legacy jsonl predating this field is not filtered
              out, and so non-fix-loop callers (default value) see the
              complete history.

    Returns a string to prepend to the retry prompt, or None if no history.
    """
    session = get_step_history(project_root, flow_id, step_id)
    if not session or not session.messages:
        return None

    # Filter by fix-iteration boundary. ``fix_iteration == 0`` is a wildcard
    # match (covers legacy data and non-fix-loop callers); otherwise must
    # match the in-flight iteration exactly.
    filtered = [
        m for m in session.messages
        if m.fix_iteration == 0
        or current_fix_iteration == 0
        or m.fix_iteration == current_fix_iteration
    ]
    if not filtered:
        return None

    # Group messages by attempt
    attempts: dict[int, list[ChatMessage]] = {}
    for msg in filtered:
        attempts.setdefault(msg.attempt, []).append(msg)

    if not attempts:
        return None

    # Set truncation limits based on mode
    assistant_fallback_limit = 4000 if mode == "continue" else 2000

    # INVARIANT: emit exactly one RETRY_HISTORY_MARKER per retry-context block.
    # LLMCaller._post_dedup_safety_cap() anchors on this marker; reusing it as
    # a section divider or emitting it twice will silently break the cap.
    parts = [RETRY_HISTORY_MARKER]

    for attempt_num in sorted(attempts.keys()):
        msgs = attempts[attempt_num]
        parts.append(f"\n=== Attempt {attempt_num + 1} ===")

        for msg in msgs:
            if msg.role == "user":
                parts.append(f"\n[User Prompt]:")
                parts.append(msg.content)

            elif msg.role == "assistant":
                # Extract full conversation from raw_json
                if msg.raw_json:
                    try:
                        conversation = extract_conversation_from_ndjson(msg.raw_json)
                    except Exception as e:
                        logger.warning(
                            f"Failed to parse raw_json for attempt {attempt_num} "
                            f"(falling back to simplified content): {e}"
                        )
                        conversation = None
                    if conversation:
                        # In 'continue' mode, check if conversation has tool calls
                        has_tool_calls = mode == "continue" and any(
                            m.tool_calls for m in conversation
                        )
                        formatted = format_conversation_for_llm(conversation)
                        # In 'continue' mode, preserve tool-call-containing responses untruncated
                        if not has_tool_calls and len(formatted) > assistant_fallback_limit:
                            head_size = min(1000, assistant_fallback_limit // 4)
                            tail_size = assistant_fallback_limit - head_size
                            formatted = formatted[:head_size] + "\n... [middle truncated, showing head+tail] ...\n" + formatted[-tail_size:]
                        parts.append(f"\n[Assistant Response]:")
                        parts.append(formatted)
                    else:
                        # Fallback to simplified content if parsing fails
                        content = msg.content
                        if len(content) > assistant_fallback_limit:
                            head_size = min(1000, assistant_fallback_limit // 4)
                            tail_size = assistant_fallback_limit - head_size
                            content = content[:head_size] + "\n... [middle truncated, showing head+tail] ...\n" + content[-tail_size:]
                        parts.append(f"\n[Assistant Response]:")
                        parts.append(content)
                else:
                    # No raw_json, use simplified content
                    content = msg.content
                    if len(content) > assistant_fallback_limit:
                        head_size = min(1000, assistant_fallback_limit // 4)
                        tail_size = assistant_fallback_limit - head_size
                        content = content[:head_size] + "\n... [middle truncated, showing head+tail] ...\n" + content[-tail_size:]
                    parts.append(f"\n[Assistant Response]:")
                    parts.append(content)

    # INVARIANT: emit exactly one RETRY_HISTORY_SEPARATOR per retry-context
    # block, immediately before the continuation notice. The cap locates the
    # tail of the retry history by rfind()-ing this sentinel; reusing it
    # elsewhere (e.g. inside [User Prompt]: content) will still resolve via
    # rfind because the outer occurrence is strictly last, but removing it
    # under any code path here will silently disable the cap.
    parts.append("\n" + RETRY_HISTORY_SEPARATOR)
    if mode == "continue":
        parts.append(
            "[Continue from where the previous attempt stopped. "
            "Do NOT redo completed work — pick up from the breakpoint.]"
        )
    else:
        parts.append("[The above attempt(s) failed. Please try again with the same task.]")
    parts.append("")

    return "\n".join(parts)


def extract_assistant_text(raw_ndjson: str) -> str:
    """Extract assistant text content from NDJSON output.

    Parses the stream-json format and extracts text from assistant messages.
    Tool calls and results are summarized briefly.
    """
    if not raw_ndjson:
        return ""

    text_parts = []
    tool_use_id_to_name: Dict[str, str] = {}
    for line in raw_ndjson.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            msg_type = data.get("type", "")

            if msg_type == "assistant":
                message = data.get("message", {})
                content = message.get("content", [])
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text = item.get("text", "")
                            if text:
                                text_parts.append(text)
                        elif item.get("type") == "tool_use":
                            name = item.get("name", "unknown")
                            tool_input = item.get("input", {})
                            tool_use_id = item.get("id", "")
                            if tool_use_id:
                                tool_use_id_to_name[tool_use_id] = name
                            preview = format_tool_use_preview(name, tool_input)
                            text_parts.append(f"[{preview}]")

            elif msg_type == "tool_result":
                result = data.get("result", {})
                content = result.get("content", "")
                is_error = result.get("isError", result.get("is_error", False))
                tool_use_id = result.get("toolUseId", result.get("tool_use_id", ""))
                tool_name = tool_use_id_to_name.get(tool_use_id, "")
                if is_error:
                    error_preview = truncate_preview(str(content)) if content else "Unknown error"
                    if tool_name:
                        text_parts.append(f"[{tool_name} ✗ {error_preview}]")
                    else:
                        text_parts.append(f"[Tool error: {error_preview}]")
                elif content:
                    preview = format_tool_result_preview(tool_name, content)
                    text_parts.append(f"[{preview}]")

        except json.JSONDecodeError:
            continue

    return "\n".join(text_parts)


def render_session_text(session: ChatSession, truncate_prompt: int = 500) -> str:
    """Render a chat session as human-readable text.

    Args:
        session: The chat session to render
        truncate_prompt: Max length for user prompts (0 = no truncation)

    Returns:
        Formatted text string
    """
    lines = [
        f"=== Step: {session.step_type} (id: {session.step_id}) ===",
        "",
    ]

    for msg in session.messages:
        ts = msg.timestamp[:19]  # Trim microseconds
        if msg.role == "user":
            lines.append(f"[User Prompt] (attempt {msg.attempt}, {ts})")
            content = msg.content
            if truncate_prompt and len(content) > truncate_prompt:
                content = content[:truncate_prompt] + "\n... [truncated]"
            lines.append(content)
        elif msg.role == "assistant":
            lines.append(f"[Assistant Response] (attempt {msg.attempt}, {ts})")
            # Render from raw_json if available, otherwise use content
            if msg.raw_json:
                rendered = _render_ndjson_for_human(msg.raw_json)
                lines.append(rendered)
            else:
                lines.append(msg.content)
        lines.append("")

    return "\n".join(lines)


def _render_ndjson_for_human(raw_ndjson: Union[str, list[dict]]) -> str:
    """Render raw NDJSON output as human-readable text.

    Distinguishes between:
    - Communication JSON (NDJSON protocol): parsed and rendered
    - LLM output JSON (e.g. analyze results): shown as-is (it's content)
    """
    parts = []

    # Handle list[dict] input (new format)
    if isinstance(raw_ndjson, list):
        json_items = raw_ndjson
    else:
        # Handle string input (NDJSON format) - parse lines
        json_items = []
        for line in raw_ndjson.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                json_items.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Track tool_use_id -> tool_name for resolving tool_result names
    tool_use_id_to_name: Dict[str, str] = {}

    # Process items
    for data in json_items:
        try:
            msg_type = data.get("type", "")

            if msg_type == "assistant":
                message = data.get("message", {})
                content = message.get("content", [])
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text = item.get("text", "")
                            if text:
                                parts.append(text)
                        elif item.get("type") == "tool_use":
                            name = item.get("name", "unknown")
                            tool_input = item.get("input", {})
                            tool_use_id = item.get("id", "")
                            if tool_use_id:
                                tool_use_id_to_name[tool_use_id] = name
                            preview = format_tool_use_preview(name, tool_input)
                            parts.append(f"[{preview}]")

            elif msg_type == "tool_result":
                result = data.get("result", {})
                content = result.get("content", "")
                is_error = result.get("isError", False)
                tool_use_id = result.get("toolUseId", "")
                tool_name = tool_use_id_to_name.get(tool_use_id, "")

                if is_error:
                    error_preview = truncate_preview(str(content)) if content else "Unknown error"
                    parts.append(f"[Result (error): {error_preview}]")
                else:
                    preview = format_tool_result_preview(tool_name, content)
                    parts.append(f"[{preview}]")

            elif msg_type == "error":
                error_msg = data.get("error", "Unknown error")
                parts.append(f"[Error] {truncate_preview(str(error_msg))}")

        except (json.JSONDecodeError, AttributeError):
            # Not protocol JSON - skip or show as-is
            continue

    return "\n".join(parts)


# Pre-compiled segment patterns for prompt segmentation (module-level to avoid
# recompilation on every call). Ordered list of (pattern, title_fn) — first
# match wins for each line.
_GENERIC_HEADING_RE = re.compile(r"^## (.+)")

_POST_SPEC_HEADING_RE = re.compile(
    r"^## (Changes Made|Planned Spec Changes|Test Results|Fix Context|Fix Instructions|Fix History|Fix Iteration|"
    r"Instructions|Previous Verification|Previous (?:Task )?Plan(?:\s*\(.*?\))?|Reviewer Feedback|"
    r"Implementation Notes|Task Type|Scope|Review Dimensions|Specifications \(for context only\)|Project Conventions|"
    r"Conflicting File.*|Design Document)"
)

_SEGMENT_PATTERNS: list[tuple[re.Pattern, Any]] = [
    # JSON mode wrapper
    (re.compile(r"^CRITICAL:\s*You MUST respond with ONLY valid JSON"),
     lambda _: "JSON Mode Instruction"),
    # Read-only constraint
    (re.compile(r"^READ-ONLY STEP CONSTRAINT|^=== READ-ONLY"),
     lambda _: "Read-Only Constraint"),
    # Language instruction
    (re.compile(r"^IMPORTANT:\s*You MUST respond in"),
     lambda _: "Language Instruction"),
    # Step template preamble ("You are an expert ...")
    (re.compile(r"^You are an expert"),
     lambda _: "Step Instructions"),
    # Discovery context
    (re.compile(r"^## Discovery Context"),
     lambda _: "Discovery Context"),
    # Available specs
    (re.compile(r"^## (Available Specifications|Available Specs)"),
     lambda _: "Available Specifications"),
    # Relevant specs
    (re.compile(r"^## Relevant Specifications"),
     lambda _: "Relevant Specifications"),
    # Project context / summary
    (re.compile(r"^## Project (Context|Summary)"),
     lambda m: f"Project {m.group(1)}"),
    # Base specification (specific pattern to avoid being swallowed by spec sections)
    (re.compile(r"^## (Base Specification.*)"),
     lambda m: m.group(1).strip()),
    # Additional user instruction
    (re.compile(r"^\[Additional user instruction\]"),
     lambda _: "Additional User Instruction"),
    # Post-spec headings from SE3 step templates — recognized even inside spec
    # sections (where the generic ## catch-all is suppressed).
    (_POST_SPEC_HEADING_RE,
     lambda m: m.group(1)),
    (re.compile(r"^## (Part \d+:.+)"), lambda m: m.group(1).strip()),
    # Generic ## sections — capture the heading text
    (_GENERIC_HEADING_RE,
     lambda m: m.group(1).strip()),
]

# Titles whose segments contain embedded spec content with internal ## headings;
# the generic ## catch-all pattern is suppressed inside these sections.
_SPEC_SECTION_TITLES = frozenset({
    "Relevant Specifications",
    "Specifications (for context only)",
    "Project Conventions",
})


def segment_prompt(prompt: str) -> list[dict[str, str]]:
    """Split a prompt into labeled segments for structured display.

    Identifies known section markers in SE3 prompts and splits the text
    into segments with auto-detected titles.

    Args:
        prompt: The raw prompt text

    Returns:
        List of {"title": str, "content": str} dicts
    """
    lines = prompt.split("\n")
    segments: list[dict[str, str]] = []
    current_title = "Prompt"
    current_lines: list[str] = []

    # Pre-compute which ### lines are real spec block starts (for auto-detection)
    _spec_block_start_lines = set()
    for m in _SPEC_BLOCK_RE.finditer(prompt):
        if _match_in_code_fence(prompt, m.start()):
            continue
        line_num = prompt[:m.start()].count("\n")
        _spec_block_start_lines.add(line_num)

    in_spec_override = False
    post_spec_seen = False

    def _flush():
        text = "\n".join(current_lines).strip()
        if text:
            segments.append({"title": current_title, "content": text})

    for line_idx, line in enumerate(lines):
        stripped = line.strip()
        matched = False

        # Auto-detect when we enter real spec content via a ### spec-name block
        if stripped.startswith("### ") and stripped[4:].strip():
            if line_idx in _spec_block_start_lines and not post_spec_seen:
                in_spec_override = True

        in_spec = (
            current_title in _SPEC_SECTION_TITLES
            or current_title.startswith("Base Specification")
            or in_spec_override
        )
        for pattern, title_fn in _SEGMENT_PATTERNS:
            if in_spec and pattern is _GENERIC_HEADING_RE:
                continue
            m = pattern.match(stripped)
            if m:
                _flush()
                current_title = title_fn(m)
                current_lines = [line]
                matched = True
                in_spec_override = False
                if pattern is _POST_SPEC_HEADING_RE:
                    post_spec_seen = True
                break
        if not matched:
            current_lines.append(line)

    _flush()
    return segments


def _format_size(size_bytes: int) -> str:
    """Format byte count as human-readable size string."""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f}MB"


_SPEC_SUBSECTION_RE = re.compile(r"^### ([\w-]+)\s*$", re.MULTILINE)

_SPEC_BLOCK_RE = re.compile(
    r"(?i)^### [\w-]+[ \t]*\r?\n"
    r"(?:[ \t]*\r?\n)*"
    r"# (?!todo\b|fixme\b|hack\b|note\b|bug\b|issue\b|warn\b|xxx\b|"
    r"config\b|import\b|from\b|def\b|class\b|return\b|if\b|for\b|while\b|"
    r"print\b|test\b)[^\s].{2,}",
    re.MULTILINE,
)


def _match_in_code_fence(text: str, match_start: int) -> bool:
    """Return True if match_start is inside an unclosed markdown code fence or indented code block."""
    before = text[:match_start]
    in_fence = False
    fence_char = None
    fence_len = 0
    for line in before.splitlines():
        stripped = line.lstrip()
        if not in_fence:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = True
                fence_char = stripped[0]
                fence_len = 0
                for ch in stripped:
                    if ch == fence_char:
                        fence_len += 1
                    else:
                        break
        else:
            if stripped.startswith(fence_char * fence_len):
                close_len = 0
                for ch in stripped:
                    if ch == fence_char:
                        close_len += 1
                    else:
                        break
                # A valid closing fence must consist only of fence chars followed by whitespace
                if close_len >= fence_len and stripped[close_len:].strip() == "":
                    in_fence = False
                    fence_char = None
                    fence_len = 0
    if in_fence:
        return True

    # Check if the line containing match_start is indented by 4+ spaces or a tab,
    # indicating an indented code block per CommonMark.
    line_start = text.rfind("\n", 0, match_start)
    if line_start == -1:
        line_start = 0
    else:
        line_start += 1
    line_prefix = text[line_start:match_start]
    return line_prefix.startswith("    ") or line_prefix.startswith("\t")


def _fold_spec_subsections(
    content: str,
    strict_starts: Optional[set] = None,
) -> Optional[list]:
    """Fold ### spec-name subsections into compact reference lines.

    Returns list of Rich Text objects, or None if no subsections found.
    """
    from rich.text import Text

    all_matches = list(_SPEC_SUBSECTION_RE.finditer(content))
    if not all_matches:
        return None

    if strict_starts is not None:
        matches = [m for m in all_matches if m.start() in strict_starts]
    else:
        matches = all_matches

    if not matches:
        return None

    result = []

    first_match = matches[0]
    preamble = content[:first_match.start()].strip()
    preamble_lines = []
    for ln in preamble.splitlines():
        stripped = ln.strip()
        if not stripped:
            continue
        # Only drop the segment title line at the very beginning;
        # preserve legitimate ## subheadings that appear later.
        if stripped.startswith("## ") and not preamble_lines:
            continue
        preamble_lines.append(ln)
    if preamble_lines:
        pre = Text()
        pre.append("\n".join(preamble_lines), style="dim")
        result.append(pre)

    last_pos = first_match.start()
    for i, match in enumerate(matches):
        if match.start() > last_pos:
            gap = content[last_pos:match.start()].strip()
            if gap:
                gap_lines = [
                    ln for ln in gap.splitlines()
                    if ln.strip()
                ]
                if gap_lines:
                    result.append(Text("\n".join(gap_lines), style="dim"))

        name = match.group(1)
        start = match.end()
        next_match = matches[i + 1] if i + 1 < len(matches) else None
        end = next_match.start() if next_match else len(content)
        sub_content = content[start:end].strip()
        size = _format_size(len(sub_content.encode("utf-8")))
        line = Text()
        line.append("[spec] ")
        line.append(f"@{name}", style="bold magenta")
        line.append(f"  (折叠, {size})", style="dim")
        result.append(line)
        last_pos = end

    if last_pos < len(content):
        trailing = content[last_pos:].strip()
        if trailing:
            trailing_lines = [
                ln for ln in trailing.splitlines()
                if ln.strip()
            ]
            if trailing_lines:
                result.append(Text("\n".join(trailing_lines), style="dim"))

    return result


def _fold_base_spec(content: str) -> Optional[list]:
    """Fold a Base Specification segment into a compact reference line.

    Returns list of Rich Text objects, or None if content is empty/placeholder.
    """
    from rich.text import Text

    lines = content.split("\n")
    body_lines = []
    for line in lines:
        if line.strip().startswith("## Base Specification"):
            continue
        body_lines.append(line)
    body = "\n".join(body_lines).strip()

    if not body or body.strip().lower() == "no base spec available":
        return None

    size = _format_size(len(body.encode("utf-8")))
    line = Text()
    line.append("[spec] ")
    line.append("@base", style="bold magenta")
    line.append(f"  (折叠, {size})", style="dim")
    return [line]


def _fold_raw_spec(content: str, label: str = "spec") -> Optional[list]:
    """Fold raw spec markdown (e.g., from sync prompts) into a compact reference line.

    Strips wrapper headings like '## Current Spec Content' or '### Current Spec Content'
    and folds the remaining body.
    """
    from rich.text import Text

    lines = content.split("\n")
    body_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## Current Spec Content") or stripped.startswith("### Current Spec Content"):
            continue
        body_lines.append(line)
    body = "\n".join(body_lines).strip()

    lower_body = body.strip().lower()
    if not lower_body or lower_body in ("not available", "(not available)"):
        return None

    size = _format_size(len(body.encode("utf-8")))
    line = Text()
    line.append("[spec] ")
    line.append(f"@{label}", style="bold magenta")
    line.append(f"  (折叠, {size})", style="dim")
    return [line]


def fold_spec_content(title: str, content: str) -> Optional[list]:
    """Dispatch spec content folding based on segment title.

    Returns list of Rich Text renderables, or None if no folding needed.
    """
    if title in ("Relevant Specifications", "Specifications (for context only)",
                  "Project Conventions"):
        strict_starts = {
            m.start() for m in _SPEC_BLOCK_RE.finditer(content)
            if not _match_in_code_fence(content, m.start())
        }
        if strict_starts:
            return _fold_spec_subsections(content, strict_starts=strict_starts)
        # Permissive fallback: under known spec titles the SE3 prompt builder
        # owns the segment and always emits proper spec structure, so any
        # `### name` subsection outside code fences is treated as a spec fold
        # even without a '# Title' H1 marker. This is intentionally asymmetric
        # with the unknown-title path below (which gates on _SPEC_BLOCK_RE.search)
        # because a benign user-authored segment would not bear these reserved
        # titles. Pinned by test_known_title_permissive_fold_no_fences_no_h1.
        outside_starts = {
            m.start() for m in _SPEC_SUBSECTION_RE.finditer(content)
            if not _match_in_code_fence(content, m.start())
        }
        if outside_starts:
            return _fold_spec_subsections(content, strict_starts=outside_starts)
        return None
    if title.startswith("Base Specification"):
        return _fold_base_spec(content)
    if title == "Current Spec Content" or title.startswith("Spec:"):
        return _fold_raw_spec(content, label="spec")
    # Fallback for old-format prompts where specs appear under non-spec titles
    if _SPEC_BLOCK_RE.search(content):
        strict_starts = {
            m.start() for m in _SPEC_BLOCK_RE.finditer(content)
            if not _match_in_code_fence(content, m.start())
        }
        if strict_starts:
            return _fold_spec_subsections(content, strict_starts=strict_starts)
        outside_starts = {
            m.start() for m in _SPEC_SUBSECTION_RE.finditer(content)
            if not _match_in_code_fence(content, m.start())
        }
        if outside_starts:
            return _fold_spec_subsections(content, strict_starts=outside_starts)
        return None
    return None


def render_session_detailed(
    session: ChatSession,
    verbose: bool = False,
) -> list:
    """Render a chat session with structured prompt and response display.

    Returns a list of Rich renderables for console output.

    Args:
        session: The chat session to render
        verbose: If True, show full response including tool calls via
                 _render_ndjson_for_human(). If False, show only the final
                 assistant text block.
    """
    from rich.text import Text
    from rich.markdown import Markdown
    from rich.console import Group

    from .display import _reverse_footer, _reverse_title

    renderables = []

    # Group messages by attempt
    attempts: dict[int, list[ChatMessage]] = {}
    for msg in session.messages:
        attempts.setdefault(msg.attempt, []).append(msg)

    for attempt_num in sorted(attempts.keys()):
        msgs = attempts[attempt_num]
        attempt_label = f"Attempt {attempt_num + 1}" if len(attempts) > 1 else ""

        for msg in msgs:
            if msg.role == "user":
                # ── Prompt: structured segment display ──
                segments = segment_prompt(msg.content)
                prompt_parts = []
                for seg in segments:
                    header = Text(f"── {seg['title']} ──", style="bold cyan")
                    prompt_parts.append(header)
                    folded = fold_spec_content(seg["title"], seg["content"])
                    if folded is not None:
                        for line in folded:
                            prompt_parts.append(line)
                    else:
                        prompt_parts.append(Markdown(seg["content"]))
                    prompt_parts.append(Text(""))  # spacer

                title = "Prompt"
                if attempt_label:
                    title = f"Prompt ({attempt_label})"
                renderables.extend([
                    _reverse_title(title, "blue"),
                    Text(""),
                    Group(*prompt_parts),
                    Text(""),
                    _reverse_footer("blue"),
                    Text(""),
                ])

            elif msg.role == "assistant":
                # ── Response display ──
                title = "Response"
                if attempt_label:
                    title = f"Response ({attempt_label})"

                if verbose and msg.raw_json:
                    # Full response with tool calls — reuse _render_ndjson_for_human
                    # Use Text() instead of Markdown() to avoid rendering artifacts
                    # (bracket sequences like [Edit: src/handler.py] get misinterpreted as links)
                    rendered_text = _render_ndjson_for_human(msg.raw_json)
                    body = Text(rendered_text) if rendered_text else Text("(empty response)", style="dim")
                    renderables.extend([
                        _reverse_title(title, "green"),
                        Text(""),
                        body,
                        Text(""),
                        _reverse_footer("green"),
                        Text(""),
                    ])
                else:
                    # Default: show only the final assistant text
                    if msg.raw_json:
                        text = _extract_final_text(msg.raw_json)
                        # If no assistant text but there was tool activity,
                        # fall back to showing tool activity summary
                        if not text:
                            text = _render_ndjson_for_human(msg.raw_json)
                    else:
                        text = msg.content
                    if text:
                        renderables.extend([
                            _reverse_title(title, "green"),
                            Text(""),
                            Markdown(text),
                            Text(""),
                            _reverse_footer("green"),
                            Text(""),
                        ])
                    else:
                        renderables.extend([
                            _reverse_title(title, "green"),
                            Text(""),
                            Text("(empty response)", style="dim"),
                            Text(""),
                            _reverse_footer("green"),
                            Text(""),
                        ])

    return renderables


def _extract_final_text(raw_json: list[dict]) -> str:
    """Extract the final text block from assistant messages in raw NDJSON.

    Returns the last text content from the last assistant message,
    skipping intermediate tool calls and tool results.
    """
    last_text = ""
    for data in raw_json:
        try:
            if data.get("type") == "assistant":
                message = data.get("message", {})
                content = message.get("content", [])
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text = item.get("text", "")
                        if text:
                            last_text = text
        except (AttributeError, TypeError):
            continue
    return last_text


def get_detailed_json(
    project_root: Path,
    flow_id: str,
) -> list[dict]:
    """Get detailed chat history data as structured JSON for a flow.

    Returns a list of step entries, each containing segmented prompt
    and full response data. Applies the same virtual-split /
    chronological interleave as the Rich display path so programmatic
    consumers see ``implement-iter{N}`` entries interleaved with
    test/self_check rather than a monolithic implement session.
    """
    sessions = get_flow_history(project_root, flow_id)
    sessions = interleave_sessions_for_display(sessions)
    result = []
    for session in sessions:
        step_data = {
            "step_id": session.step_id,
            "step_type": session.step_type,
            "messages": [],
        }
        for msg in session.messages:
            msg_data: dict = {
                "role": msg.role,
                "attempt": msg.attempt,
                "timestamp": msg.timestamp,
            }
            if msg.role == "user":
                msg_data["segments"] = segment_prompt(msg.content)
                msg_data["content"] = msg.content
            elif msg.role == "assistant":
                msg_data["content"] = msg.content
                msg_data["raw_json"] = msg.raw_json
            step_data["messages"].append(msg_data)
        result.append(step_data)
    return result


def _append_message(
    project_root: Path, flow_id: str, step_id: str, msg: ChatMessage
) -> None:
    """Append a message to the history file."""
    path = _history_file(project_root, flow_id, step_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"Failed to write chat history: {e}")
