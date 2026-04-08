"""Chat history system for LLM call tracking.

Records prompts and responses for each flow step, enables retry context
injection, and provides human-readable browsing of conversation history.

Storage format: se3/history/{flow_id}/{step_id}.jsonl
Each line is a JSON-serialized ChatMessage.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

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
) -> None:
    """Record a user prompt sent to the LLM."""
    msg = ChatMessage(
        role="user",
        content=prompt,
        raw_json=[],
        timestamp=datetime.now().isoformat(),
        step_type=step_type,
        attempt=attempt,
    )
    _append_message(project_root, flow_id, step_id, msg)


def record_response(
    project_root: Path,
    flow_id: str,
    step_id: str,
    step_type: str,
    raw_ndjson: str,
    attempt: int,
) -> None:
    """Record an LLM response (raw NDJSON output)."""
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
    )
    _append_message(project_root, flow_id, step_id, msg)


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
            msg = ChatMessage.from_dict(data)
            messages.append(msg)
            if not step_type:
                step_type = msg.step_type
        except (json.JSONDecodeError, KeyError) as e:
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
                    if len(content) > 500:
                        content = content[:500] + "\n... [truncated]"
                    status = " (error)" if tr.get("is_error") else ""
                    parts.append(f"[Tool Result{status}]: {content}")

        parts.append("")  # Empty line between messages

    return "\n".join(parts)


def format_history_for_retry(
    project_root: Path, flow_id: str, step_id: str, mode: str = "continue"
) -> Optional[str]:
    """Format previous conversation attempts for retry context injection.

    Extracts the full conversation from raw NDJSON to preserve tool calls
    and results structure, rather than using simplified text summaries.

    Args:
        project_root: Project root directory
        flow_id: Flow instance ID
        step_id: Step instance ID
        mode: 'continue' (default) to resume from breakpoint, or 'retry' to restart.
              In 'continue' mode: higher truncation limits (4000 chars for user prompts),
              assistant responses with tool calls are not truncated, and a continuation
              instruction is appended. In 'retry' mode: preserves original behavior.

    Returns a string to prepend to the retry prompt, or None if no history.
    """
    session = get_step_history(project_root, flow_id, step_id)
    if not session or not session.messages:
        return None

    # Group messages by attempt
    attempts: dict[int, list[ChatMessage]] = {}
    for msg in session.messages:
        attempts.setdefault(msg.attempt, []).append(msg)

    if not attempts:
        return None

    # Set truncation limits based on mode
    user_prompt_limit = 4000 if mode == "continue" else 2000
    assistant_fallback_limit = 4000 if mode == "continue" else 2000

    parts = ["[Previous conversation context for this step]:"]

    for attempt_num in sorted(attempts.keys()):
        msgs = attempts[attempt_num]
        parts.append(f"\n=== Attempt {attempt_num + 1} ===")

        for msg in msgs:
            if msg.role == "user":
                # Truncate very long prompts in context
                content = msg.content
                if len(content) > user_prompt_limit:
                    content = content[:user_prompt_limit] + "\n... [truncated]"
                parts.append(f"\n[User Prompt]:")
                parts.append(content)

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
                            formatted = formatted[:assistant_fallback_limit] + "\n... [truncated]"
                        parts.append(f"\n[Assistant Response]:")
                        parts.append(formatted)
                    else:
                        # Fallback to simplified content if parsing fails
                        content = msg.content
                        if len(content) > assistant_fallback_limit:
                            content = content[:assistant_fallback_limit] + "\n... [truncated]"
                        parts.append(f"\n[Assistant Response]:")
                        parts.append(content)
                else:
                    # No raw_json, use simplified content
                    content = msg.content
                    if len(content) > assistant_fallback_limit:
                        content = content[:assistant_fallback_limit] + "\n... [truncated]"
                    parts.append(f"\n[Assistant Response]:")
                    parts.append(content)

    parts.append("\n" + "=" * 40)
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
                tool_use_id = result.get("toolUseId", "")
                tool_name = tool_use_id_to_name.get(tool_use_id, "")
                if content:
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
