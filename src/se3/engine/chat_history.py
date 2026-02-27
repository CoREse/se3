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
from typing import List, Optional

logger = logging.getLogger(__name__)

# Default project root for history storage
_SE3_DIR = "se3"
_HISTORY_DIR = "history"


@dataclass
class ChatMessage:
    """A single message in a chat session."""

    role: str  # "user" | "assistant"
    content: str  # Parsed text content
    raw_ndjson: str  # Original NDJSON output (assistant only)
    timestamp: str  # ISO format
    step_type: str  # e.g. "analyze", "propose"
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
        raw_ndjson="",
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
    msg = ChatMessage(
        role="assistant",
        content=text,
        raw_ndjson=raw_ndjson,
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


def format_history_for_retry(
    project_root: Path, flow_id: str, step_id: str
) -> Optional[str]:
    """Format previous conversation attempts for retry context injection.

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

    parts = ["[Previous conversation context for this step]:"]
    for attempt_num in sorted(attempts.keys()):
        msgs = attempts[attempt_num]
        parts.append("---")
        for msg in msgs:
            if msg.role == "user":
                # Truncate very long prompts in context
                content = msg.content
                if len(content) > 2000:
                    content = content[:2000] + "\n... [truncated]"
                parts.append(f"User prompt: {content}")
            elif msg.role == "assistant":
                content = msg.content
                if len(content) > 2000:
                    content = content[:2000] + "\n... [truncated]"
                parts.append(f"Assistant response: {content}")
        parts.append("---")

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
                            text_parts.append(f"[Tool Call: {name}]")

            elif msg_type == "tool_result":
                result = data.get("result", {})
                content = result.get("content", "")
                if content:
                    preview = str(content)[:200]
                    text_parts.append(f"[Tool Result: {preview}]")

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
            # Render from raw_ndjson if available, otherwise use content
            if msg.raw_ndjson:
                rendered = _render_ndjson_for_human(msg.raw_ndjson)
                lines.append(rendered)
            else:
                lines.append(msg.content)
        lines.append("")

    return "\n".join(lines)


def _render_ndjson_for_human(raw_ndjson: str) -> str:
    """Render raw NDJSON output as human-readable text.

    Distinguishes between:
    - Communication JSON (NDJSON protocol): parsed and rendered
    - LLM output JSON (e.g. analyze results): shown as-is (it's content)
    """
    parts = []
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
                                parts.append(text)
                        elif item.get("type") == "tool_use":
                            name = item.get("name", "unknown")
                            tool_input = item.get("input", {})
                            input_preview = json.dumps(tool_input)[:200]
                            parts.append(f"[Tool Call: {name}] {input_preview}")

            elif msg_type == "tool_result":
                result = data.get("result", {})
                content = result.get("content", "")
                if content:
                    preview = str(content)[:300]
                    parts.append(f"[Tool Result] {preview}")
                else:
                    parts.append("[Tool Result] (empty)")

            elif msg_type == "error":
                error_msg = data.get("error", "Unknown error")
                parts.append(f"[Error] {error_msg}")

        except json.JSONDecodeError:
            # Not protocol JSON - show as-is
            parts.append(line)

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
