"""OpenAI Codex CLI adapter (single-command runner).

Provides a unified way to invoke the Codex CLI (``codex exec --json``) from
SE3 modules.  Each ``CodexRunner`` instance wraps a single agent command.
Agent selection/rotation is handled by :class:`LLMCaller`.

Design principle (from the user): LLM-agnostic parts use the shared
stream-json NDJSON system borrowed from Claude; LLM-specific parts (CLI
argument construction, output parsing) are owned by each runner.

The :class:`CodexEventConverter` translates codex's JSONL events
(``thread.started``, ``turn.started/completed/failed``, ``item.*``) into
Claude-compatible stream-json NDJSON so that ``StreamJSONTracker``, chat
history, retry/continue context, web console, ``last_raw_result``,
``_last_touched_files``, and all other upper-layer consumers work unchanged.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import select
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

from .agent_runner import AgentInvocationIntent, AgentRunner, InfraErrorType

# Platform-specific imports for process resource monitoring
try:
    if sys.platform.startswith("linux"):
        import psutil
    else:
        psutil = None
except ImportError:
    psutil = None

logger = logging.getLogger(__name__)

# Threshold for routing prompt via stdin instead of as a positional argument.
# Linux MAX_ARG_STRLEN is 128 KB; 100 KB leaves ~28 KB safety margin.
_MAX_ARG_BYTES = 102400

# Keywords indicating usage/rate limit in codex output
_USAGE_LIMIT_KEYWORDS = [
    "usage limit",
    "rate limit",
    "too many requests",
    "429",
    "quota exceeded",
]

# Keywords indicating authentication failure
_AUTH_FAILURE_KEYWORDS = [
    "401",
    "unauthorized",
    "authentication failed",
]

# Patterns indicating shell snapshot validation failure in Codex stderr.
# Matched case-insensitively against the tail of stderr output.
_SHELL_SNAPSHOT_PATTERNS = [
    "shell snapshot validation failed",
    "codex_core::shell_snapshot",
    "syntax error near unexpected token",
]

# Maximum number of stderr lines to retain in the bounded buffer.
_STDERR_BUFFER_MAXLEN = 100

# codex item ``status`` values that mean the item will not change again.
_TERMINAL_ITEM_STATUSES = frozenset({"completed", "failed"})

# Private key used to memoize a synthesized tool id on an item that carries no
# id of its own.  Written into the parsed event dict only, which is local to
# one convert_line call chain and never re-serialized.
_SYNTHETIC_ID_KEY = "_tianluo_synthetic_tool_id"


def _detect_shell_snapshot_failure(stderr_text: str) -> bool:
    """Check if stderr contains a shell snapshot validation failure.

    Matches against patterns like:
        codex_core::shell_snapshot: Shell snapshot validation failed ...
        syntax error near unexpected token '('

    Args:
        stderr_text: The captured stderr content (may be empty).

    Returns:
        True if a shell snapshot failure pattern is detected.
    """
    if not stderr_text:
        return False
    lower = stderr_text.lower()
    return any(pattern in lower for pattern in _SHELL_SNAPSHOT_PATTERNS)


class CodexEventConverter:
    """Convert codex ``--json`` JSONL events to Claude stream-json NDJSON.

    Each call to :meth:`convert_line` processes one JSONL line from codex's
    stdout and returns zero or more Claude-compatible NDJSON lines.  The
    converter maintains internal state (accumulated agent messages, tool-use
    ids) across the lifetime of a single codex invocation.

    Unknown event types and non-JSON lines are logged and silently produce no
    output — the converter never raises.
    """

    def __init__(self, provider: Optional[str] = "openai") -> None:
        # WHY: keyed by item id rather than a plain list because codex pushes
        # the same agent_message several times (item.started/updated/completed)
        # while it fills in; last write per id wins, and dict order preserves
        # arrival order for the joined result text.
        self._agent_messages: Dict[str, str] = {}
        self._seen_turn_terminal: bool = False
        self._tool_counter: int = 0
        self._touched_files: set = set()
        # WHY: codex re-sends one item across started/updated/completed events.
        # A chip is per *item*, not per event, so emission is gated on these id
        # sets: one tool_use on first sight, one tool_result on the terminal
        # sighting.  _inflight_tools additionally remembers the tool name so a
        # turn that ends mid-item can synthesize a closing tool_result.
        self._emitted_tool_use: set = set()
        self._emitted_tool_result: set = set()
        self._inflight_tools: Dict[str, str] = {}
        self._provider: Optional[str] = provider
        self._provider_session_id: Optional[str] = None
        self._reported_model: Optional[str] = None

    @property
    def touched_files(self) -> set:
        """Set of file paths from ``file_change`` items."""
        return self._touched_files

    def _next_tool_id(self) -> str:
        self._tool_counter += 1
        return f"codex_tool_{self._tool_counter}"

    def _resolve_item_id(self, item: Any) -> str:
        """Return a stable id for *item* (``call_id`` → ``id`` → synthesized).

        Repeated calls for the same item object return the same id: the
        synthesized fallback is memoized on the item itself so one item can
        never end up split across two ids (which would produce two chips).
        """
        if not isinstance(item, dict):
            return self._next_tool_id()

        for key in ("call_id", "id"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, int) and not isinstance(value, bool):
                return str(value)

        cached = item.get(_SYNTHETIC_ID_KEY)
        if isinstance(cached, str):
            return cached
        synthetic = self._next_tool_id()
        item[_SYNTHETIC_ID_KEY] = synthetic
        return synthetic

    @staticmethod
    def _is_terminal_item(event_type: str, item: Any) -> bool:
        """True when *item* has reached its final state.

        Both signals are accepted — the ``item.completed`` event type and a
        terminal ``status`` on the item — because codex may express the end of
        an item with either, and an item stuck as in-flight would leave a chip
        dangling forever.
        """
        if event_type == "item.completed":
            return True
        if not isinstance(item, dict):
            return False
        status = item.get("status")
        if not isinstance(status, str):
            return False
        return status.strip().lower() in _TERMINAL_ITEM_STATUSES

    def convert_line(self, line: str) -> List[str]:
        """Convert a single codex JSONL line to zero or more Claude NDJSON lines.

        Returns:
            List of JSON-serialized NDJSON strings.  Empty list means the
            line was consumed but produced no output (e.g. ``thread.started``).
        """
        stripped = line.strip()
        if not stripped:
            return []

        try:
            event = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            logger.debug("CodexEventConverter: non-JSON line ignored: %s", stripped[:200])
            return []

        event_type = event.get("type", "")

        try:
            if event_type in ("thread.started", "turn.started"):
                return self._handle_metadata_event(event_type, event)

            if event_type in ("item.started", "item.updated", "item.completed"):
                return self._handle_item_event(event_type, event)

            if event_type == "turn.completed":
                return self._handle_turn_completed(self._merged_event(event))

            if event_type in ("turn.failed", "error"):
                return self._handle_turn_failed(self._merged_event(event))

            # Unknown event type — log and skip
            logger.debug(
                "CodexEventConverter: unknown event type %r ignored",
                event_type,
            )
            return []
        except Exception:
            logger.debug(
                "CodexEventConverter: error processing %r event",
                event_type,
                exc_info=True,
            )
            return []

    @staticmethod
    def _merged_event(event: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(event)
        nested = event.get("data")
        if isinstance(nested, dict):
            merged.update(nested)
        return merged

    @staticmethod
    def _metadata_value(
        data: Dict[str, Any], keys: tuple[str, ...]
    ) -> Optional[str]:
        containers = [data]
        for name in ("thread", "turn", "session", "message", "response"):
            nested = data.get(name)
            if isinstance(nested, dict):
                containers.append(nested)
        for container in containers:
            for key in keys:
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    def _update_metadata(
        self, data: Dict[str, Any], *, thread_started: bool = False
    ) -> Dict[str, Any]:
        provider = self._metadata_value(data, ("provider", "provider_name"))
        model = self._metadata_value(data, ("model", "model_name"))
        session_id = self._metadata_value(
            data, ("provider_session_id", "session_id", "thread_id")
        )
        if session_id is None and thread_started:
            session_id = self._metadata_value(data, ("id",))
        if provider is not None:
            self._provider = provider
        if model is not None:
            self._reported_model = model
        if session_id is not None:
            self._provider_session_id = session_id

        metadata: Dict[str, Any] = {}
        if provider is not None:
            metadata["provider"] = provider
        if model is not None:
            metadata["model"] = model
        if session_id is not None:
            metadata["provider_session_id"] = session_id
        return metadata

    def _handle_metadata_event(
        self, event_type: str, event: Dict[str, Any]
    ) -> List[str]:
        data = self._merged_event(event)
        metadata = self._update_metadata(
            data, thread_started=event_type == "thread.started"
        )
        if not metadata:
            return []
        return [json.dumps({"type": "init", **metadata}, ensure_ascii=False)]

    @staticmethod
    def _usage_payload(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        candidates = [data.get("usage")]
        for name in ("message", "turn", "response"):
            nested = data.get(name)
            candidates.append(
                nested.get("usage") if isinstance(nested, dict) else None
            )
        raw = next((item for item in candidates if isinstance(item, dict)), None)
        if raw is None:
            return None

        usage: Dict[str, Any] = {}
        field_aliases = {
            "input_tokens": ("input_tokens", "prompt_tokens"),
            "output_tokens": ("output_tokens", "completion_tokens"),
            # WHY: the two provider shapes disagree on whether input_tokens
            # contains the cached tokens, and the shared normalizer decides by
            # field name. Renaming cached_input_tokens to the Anthropic key
            # would make an OpenAI-shape subset be normalized as an additive
            # exclusive category and double-bill it — so each shape's marker
            # field is passed through unchanged.
            "cached_input_tokens": ("cached_input_tokens",),
            "cache_read_input_tokens": ("cache_read_input_tokens",),
            "cache_creation_input_tokens": ("cache_creation_input_tokens",),
            "cache_creation_5m_input_tokens": (
                "cache_creation_5m_input_tokens",
                "cache_creation_5_minute_input_tokens",
            ),
            "cache_creation_1h_input_tokens": (
                "cache_creation_1h_input_tokens",
                "cache_creation_1_hour_input_tokens",
            ),
        }
        for target, aliases in field_aliases.items():
            for alias in aliases:
                if alias in raw:
                    usage[target] = raw[alias]
                    break
        details = raw.get("input_tokens_details")
        if isinstance(details, dict):
            usage["input_tokens_details"] = dict(details)
        return usage

    @staticmethod
    def _cost_payload(data: Dict[str, Any]) -> tuple[bool, Any]:
        for container in (
            data,
            data.get("turn") if isinstance(data.get("turn"), dict) else {},
            data.get("message") if isinstance(data.get("message"), dict) else {},
            data.get("response") if isinstance(data.get("response"), dict) else {},
        ):
            for key in ("actual_cost_usd", "total_cost_usd", "cost_usd"):
                if key in container and container.get(key) is not None:
                    return True, container.get(key)
        return False, None

    def _apply_terminal_metadata(
        self, result_event: Dict[str, Any], data: Dict[str, Any]
    ) -> None:
        explicit = self._update_metadata(data)
        provider = explicit.get("provider", self._provider)
        model = explicit.get("model", self._reported_model)
        session_id = explicit.get(
            "provider_session_id", self._provider_session_id
        )
        usage_event_id = self._metadata_value(
            data,
            ("usage_event_id", "request_id", "response_id", "turn_id"),
        )
        if usage_event_id is None:
            turn = data.get("turn")
            if isinstance(turn, dict):
                usage_event_id = self._metadata_value(turn, ("id",))
        if usage_event_id is None and str(data.get("type", "")).startswith(
            "turn."
        ):
            usage_event_id = self._metadata_value(data, ("id",))
        if provider is not None:
            result_event["provider"] = provider
        if model is not None:
            result_event["model"] = model
        if session_id is not None:
            result_event["provider_session_id"] = session_id
        if usage_event_id is not None:
            result_event["usage_event_id"] = usage_event_id
        for key in ("usage_semantics", "cost_semantics"):
            if key in data:
                result_event[key] = data[key]
    def _tool_use_line(
        self, tool_use_id: str, tool_name: str, tool_input: Dict[str, Any]
    ) -> str:
        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_use_id,
                        "name": tool_name,
                        "input": tool_input,
                    }
                ],
            },
        }
        return json.dumps(event, ensure_ascii=False)

    def _tool_result_line(
        self, tool_use_id: str, content: str, is_error: bool
    ) -> str:
        event = {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": content,
                        "is_error": is_error,
                    }
                ],
            },
        }
        return json.dumps(event, ensure_ascii=False)

    def _emit_tool_events(
        self,
        *,
        tool_use_id: str,
        tool_name: str,
        tool_input: Dict[str, Any],
        result_content: str,
        is_error: bool,
        is_terminal: bool,
    ) -> List[str]:
        """Emit the tool_use / tool_result pair for one item, deduped by id.

        WHY: codex reports one item several times as it progresses, so emission
        is keyed on the item id — the tool_use goes out on first sight (giving
        an in-flight chip) and the tool_result only once the item is final.  A
        tool_result built from a mid-flight event would show truncated output
        and, on the next event, a second chip for the same command.
        """
        lines: List[str] = []

        if tool_use_id not in self._emitted_tool_use:
            self._emitted_tool_use.add(tool_use_id)
            self._inflight_tools[tool_use_id] = tool_name
            lines.append(self._tool_use_line(tool_use_id, tool_name, tool_input))

        if is_terminal and tool_use_id not in self._emitted_tool_result:
            self._emitted_tool_result.add(tool_use_id)
            self._inflight_tools.pop(tool_use_id, None)
            lines.append(
                self._tool_result_line(tool_use_id, result_content, is_error)
            )

        return lines

    @staticmethod
    def _status_text(item: Dict[str, Any]) -> str:
        """Lowercased ``status`` of *item*, or ``""`` when absent/non-string."""
        status = item.get("status")
        return status.strip().lower() if isinstance(status, str) else ""

    @staticmethod
    def _stringify(value: Any) -> str:
        """Render an arbitrary item payload value as chip-displayable text.

        Structured payloads go through ``json.dumps`` rather than ``str`` so a
        chip shows valid JSON instead of Python ``repr`` punctuation.
        """
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            try:
                return json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                return str(value)
        return str(value)

    @classmethod
    def _join_argv(cls, values: List[Any]) -> str:
        """Flatten a codex argv / ``parsed_cmd`` list into one command string."""
        parts: List[str] = []
        for value in values:
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, dict):
                for key in ("command", "cmd"):
                    nested = value.get(key)
                    if isinstance(nested, str):
                        parts.append(nested)
                        break
                    if isinstance(nested, list):
                        parts.append(cls._join_argv(nested))
                        break
        return " ".join(part for part in parts if part)

    @classmethod
    def _command_text(cls, item: Dict[str, Any]) -> str:
        """Command string of a ``command_execution`` item.

        WHY: ``command`` → ``parsed_cmd`` rather than a codex-version check.
        The codex JSON schema is not versioned and does not move with tianluo's
        releases, so probing ``codex --version`` would buy a mapping table that
        is still wrong for any version nobody tested; a fallback chain is local,
        free, and degrades on its own for shapes we have never seen.
        """
        command = item.get("command")
        if isinstance(command, str) and command:
            return command
        if isinstance(command, list):
            joined = cls._join_argv(command)
            if joined:
                return joined
        parsed = item.get("parsed_cmd")
        if isinstance(parsed, str):
            return parsed
        if isinstance(parsed, list):
            return cls._join_argv(parsed)
        return command if isinstance(command, str) else ""

    @staticmethod
    def _command_output(item: Dict[str, Any]) -> str:
        """Captured output of a ``command_execution`` item.

        WHY: the real codex field is ``aggregated_output`` — there is no
        ``output`` key in the exec schema at all, and reading it is what made
        every Bash chip render as "0 lines output".  ``output`` and the
        stdout/stderr pair are kept behind it as fallbacks for older/other
        shapes; see :meth:`_command_text` for why this is a fallback chain
        rather than a version check.
        """
        for key in ("aggregated_output", "output"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value

        stdout = item.get("stdout") if isinstance(item.get("stdout"), str) else ""
        stderr = item.get("stderr") if isinstance(item.get("stderr"), str) else ""
        if stdout and stderr:
            separator = "" if stdout.endswith("\n") else "\n"
            return f"{stdout}{separator}{stderr}"
        return stdout or stderr

    @classmethod
    def _command_is_error(cls, item: Dict[str, Any]) -> bool:
        """True when a ``command_execution`` item reports failure.

        A missing ``exit_code`` (the item is still running, or codex simply did
        not report one) is not evidence of failure, so only an explicit
        non-zero integer counts.
        """
        if cls._status_text(item) == "failed":
            return True
        exit_code = item.get("exit_code")
        if isinstance(exit_code, bool):
            return False
        return isinstance(exit_code, int) and exit_code != 0

    @staticmethod
    def _change_path(change: Dict[str, Any]) -> str:
        for key in ("path", "file_path"):
            value = change.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    @staticmethod
    def _map_change_kind(kind: str) -> str:
        """Map a codex file-change ``kind`` to the closest Claude tool name.

        ``Delete`` is not a registered Claude tool; the generic formatter
        renders it by ``file_path``, which is exactly what codex gives us.
        """
        normalized = kind.strip().lower()
        if normalized in ("add", "create", "write"):
            return "Write"
        if normalized in ("delete", "remove"):
            return "Delete"
        return "Edit"

    def _handle_file_changes(
        self, item: Dict[str, Any], changes: List[Any], is_terminal: bool
    ) -> List[str]:
        """Emit one tool_use/tool_result pair per entry of ``changes``.

        WHY: the ids carry a per-change suffix because all changes of one item
        share that item's id — without the suffix they would collapse into a
        single chip and only the first file would ever be shown.
        """
        base_id = self._resolve_item_id(item)
        results: List[str] = []

        for index, change in enumerate(changes):
            if not isinstance(change, dict):
                continue
            path = self._change_path(change)
            if not path:
                continue
            kind = change.get("kind", "")
            kind_text = kind.strip().lower() if isinstance(kind, str) else ""
            self._touched_files.add(path)

            results.extend(
                self._emit_tool_events(
                    tool_use_id=f"{base_id}#{index}",
                    tool_name=self._map_change_kind(kind_text),
                    # WHY: codex never sends file content, and an empty
                    # ``content``/``new_string`` would be rendered as a real
                    # zero-line write.  Omitting the key entirely lets the whole
                    # render path — chip header, detail panel and CLI diff — tell
                    # "no content information" from "empty file"; note the
                    # renderers' old-side snapshot is the *post-write* file, so a
                    # synthesized diff would show the new file as fully deleted.
                    tool_input={"file_path": path},
                    result_content=f"File {kind_text or 'change'}: {path}",
                    is_error=False,
                    is_terminal=is_terminal,
                )
            )

        return results

    def _handle_item_event(self, event_type: str, event: Dict[str, Any]) -> List[str]:
        """Handle ``item.started`` / ``item.updated`` / ``item.completed``.

        Shapes below are the real ``codex exec --json`` schema, read off the
        serde string pool of the codex-cli 0.147.0 exec crate — note in
        particular that the pool contains **no** ``output`` key.

        Thread events: ``thread.started``, ``turn.started``, ``turn.completed``,
        ``turn.failed``, ``item.started``, ``item.updated``, ``item.completed``.
        Item payloads are nested under the ``item`` key and have one of seven
        types — ``agent_message``, ``reasoning``, ``command_execution``,
        ``file_change``, ``mcp_tool_call``, ``web_search``, ``todo_list``::

            {"type": "item.completed", "item": {
                "id": "item_0", "type": "agent_message", "text": "Hello"}}
            {"type": "item.completed", "item": {
                "id": "item_1", "type": "command_execution", "command": "ls",
                "aggregated_output": "a\\nb", "exit_code": 0,
                "status": "completed"}}
            {"type": "item.completed", "item": {
                "id": "item_2", "type": "file_change", "status": "completed",
                "changes": [{"path": "/tmp/x.py", "kind": "update"}]}}
            {"type": "item.completed", "item": {
                "id": "item_3", "type": "mcp_tool_call", "server": "docs",
                "tool": "search", "arguments": "{}", "result": {...},
                "status": "completed"}}
            {"type": "item.completed", "item": {
                "id": "item_4", "type": "web_search", "query": "..."}}
            {"type": "item.completed", "item": {
                "id": "item_5", "type": "todo_list", "items": [...]}}

        Value domains: ``changes[].kind`` is one of ``add`` / ``delete`` /
        ``update``; ``status`` is one of ``in_progress`` / ``completed`` /
        ``failed`` / ``running`` / ``interrupted`` / ``errored`` /
        ``not_found``.

        Every field is read through a tolerant multi-key fallback chain (real
        key first, historical key next) — see :meth:`_command_text` for why
        that is preferred over detecting the codex version.

        The same item arrives once per lifecycle stage carrying the same id;
        emission is deduped per id by :meth:`_emit_tool_events`.
        """
        results: List[str] = []

        # Codex items are nested under the "item" key; fall back to the whole
        # event for forward-compat with possible schema variants.
        item = event.get("item", event)
        if not isinstance(item, dict):
            return results
        item_type = item.get("type", "")
        is_terminal = self._is_terminal_item(event_type, item)

        if item_type == "agent_message":
            text = item.get("text", "")
            if text:
                # WHY: the same message id is re-sent with progressively more
                # text; storing by id (last write wins) keeps the joined result
                # text free of duplicated fragments.
                self._agent_messages[self._resolve_item_id(item)] = text
                # WHY: only the final text becomes an assistant event — a
                # half-streamed fragment emitted as assistant would land in
                # history as if it were a complete answer.
                if is_terminal:
                    assistant_event = {
                        "type": "assistant",
                        "message": {
                            "content": [{"type": "text", "text": text}],
                        },
                    }
                    results.append(json.dumps(assistant_event, ensure_ascii=False))

        elif item_type == "command_execution":
            results.extend(
                self._emit_tool_events(
                    tool_use_id=self._resolve_item_id(item),
                    tool_name="Bash",
                    tool_input={"command": self._command_text(item)},
                    result_content=self._command_output(item),
                    is_error=self._command_is_error(item),
                    is_terminal=is_terminal,
                )
            )

        elif item_type == "file_change":
            # WHY: the real schema is a `changes` list of {path, kind}; the flat
            # path/content/change_type reading below it is the historical shape,
            # kept as a fallback for the same reason the other fields use
            # fallback chains instead of a codex-version check.
            if "changes" in item:
                changes = item.get("changes")
                if isinstance(changes, list):
                    results.extend(
                        self._handle_file_changes(item, changes, is_terminal)
                    )
                else:
                    # A `changes` key of the wrong type carries no usable path;
                    # a chip built from it would just show wrong information.
                    logger.debug(
                        "CodexEventConverter: file_change `changes` is %s, "
                        "not a list — item skipped",
                        type(changes).__name__,
                    )
            else:
                path = item.get("path", item.get("file_path", ""))
                content = item.get("content", "")
                change_type = item.get("change_type", "write")

                mapped_name = "Write" if change_type in ("write", "create") else "Edit"
                tool_input: Dict[str, Any] = {"file_path": path}
                if change_type in ("write", "create"):
                    tool_input["content"] = content
                else:
                    tool_input["new_string"] = content

                # Record touched file for dependency tracking
                if path:
                    self._touched_files.add(path)

                results.extend(
                    self._emit_tool_events(
                        tool_use_id=self._resolve_item_id(item),
                        tool_name=mapped_name,
                        tool_input=tool_input,
                        result_content=f"File {change_type}: {path}",
                        is_error=False,
                        is_terminal=is_terminal,
                    )
                )

        elif item_type == "mcp_tool_call":
            # WHY: `tool` is the real key, `name` the historical one — same
            # fallback-over-version-detection reasoning as _command_text.  The
            # mcp__{server}__{tool} spelling is Claude's own MCP naming
            # convention, so codex MCP calls flow through the existing
            # rendering and statistics paths without a codex-specific branch.
            tool_name = ""
            for key in ("tool", "name"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    tool_name = value.strip()
                    break
            server = item.get("server")
            if isinstance(server, str) and server.strip() and tool_name:
                tool_name = f"mcp__{server.strip()}__{tool_name}"
            elif not tool_name:
                tool_name = "unknown"

            arguments = item.get("arguments", item.get("input", {}))
            if isinstance(arguments, str):
                try:
                    tool_input = json.loads(arguments)
                except (json.JSONDecodeError, ValueError):
                    tool_input = {"raw": arguments}
                if not isinstance(tool_input, dict):
                    tool_input = {"raw": arguments}
            elif isinstance(arguments, dict):
                tool_input = arguments
            else:
                tool_input = {} if arguments is None else {"raw": arguments}

            result = item.get("result", item.get("output", ""))
            is_error = self._status_text(item) == "failed" or (
                isinstance(result, dict) and bool(result.get("isError"))
            )

            results.extend(
                self._emit_tool_events(
                    tool_use_id=self._resolve_item_id(item),
                    tool_name=tool_name,
                    tool_input=tool_input,
                    # An empty result is a legitimate MCP answer; suppressing
                    # the tool_result for it would leave the chip in-flight
                    # until the turn ended.
                    result_content=self._stringify(result),
                    is_error=is_error,
                    is_terminal=is_terminal,
                )
            )

        elif item_type == "web_search":
            query = item.get("query")
            # Without the query there is nothing truthful to put on the chip —
            # a placeholder chip is worse than no chip.
            if isinstance(query, str) and query:
                search_result = item.get("results", item.get("action", ""))
                results.extend(
                    self._emit_tool_events(
                        tool_use_id=self._resolve_item_id(item),
                        tool_name="WebSearch",
                        tool_input={"query": query},
                        result_content=self._stringify(search_result),
                        is_error=self._status_text(item) == "failed",
                        is_terminal=is_terminal,
                    )
                )

        elif item_type == "todo_list":
            todo_items = item.get("items")
            if isinstance(todo_items, list) and todo_items:
                results.extend(
                    self._emit_tool_events(
                        tool_use_id=self._resolve_item_id(item),
                        tool_name="TodoWrite",
                        tool_input={"items": todo_items},
                        result_content="",
                        is_error=False,
                        is_terminal=is_terminal,
                    )
                )

        # WHY: `reasoning` is deliberately dropped — it is the model's internal
        # thinking, and letting it reach _agent_messages would pollute the
        # result text that history, self_check and commit messages all treat as
        # the final deliverable.

        return results

    @staticmethod
    def _map_tool_name(codex_name: str) -> str:
        """Map codex function/tool names to Claude tool names.

        Codex uses generic names like ``shell``, ``apply_patch`` etc.
        We map them to the closest Claude equivalents so the upper-layer
        tool formatters and progress renderers can display meaningful
        previews.
        """
        mapping = {
            "shell": "Bash",
            "bash": "Bash",
            "execute": "Bash",
            "apply_patch": "Edit",
            "write_file": "Write",
            "read_file": "Read",
            "list_files": "Glob",
            "search_files": "Grep",
        }
        return mapping.get(codex_name, codex_name)

    def _close_inflight_tools(self) -> List[str]:
        """Synthesize a closing tool_result for every still-in-flight item.

        WHY: a tool_use whose tool_result never arrives leaves the chip
        rendering as running forever.  When the turn ends, whatever is still
        open will never complete, so it is closed explicitly as interrupted.
        """
        lines: List[str] = []
        for tool_use_id, tool_name in list(self._inflight_tools.items()):
            if tool_use_id in self._emitted_tool_result:
                continue
            self._emitted_tool_result.add(tool_use_id)
            lines.append(
                self._tool_result_line(
                    tool_use_id,
                    f"[interrupted] {tool_name} did not complete before the "
                    f"turn ended",
                    True,
                )
            )
        self._inflight_tools.clear()
        return lines

    def _reset_turn_state(self) -> None:
        """Drop per-turn lifecycle state so a following turn starts clean."""
        self._agent_messages = {}
        self._emitted_tool_use.clear()
        self._emitted_tool_result.clear()
        self._inflight_tools.clear()

    def _result_text(self) -> str:
        """Join the accumulated agent messages in arrival order."""
        return "\n".join(self._agent_messages.values())

    def _handle_turn_completed(self, data: Dict[str, Any]) -> List[str]:
        """Handle ``turn.completed`` — emit a ``type: result`` event."""
        self._seen_turn_terminal = True

        # Close dangling chips before the terminal event so consumers see the
        # results while the tool_use ids are still meaningful.
        lines = self._close_inflight_tools()

        result_event = {
            "type": "result",
            "result": self._result_text(),
        }
        usage = self._usage_payload(data)
        if usage is not None:
            result_event["usage"] = usage
        cost_seen, cost = self._cost_payload(data)
        if cost_seen:
            result_event["total_cost_usd"] = cost
        self._apply_terminal_metadata(result_event, data)
        self._reset_turn_state()
        lines.append(json.dumps(result_event, ensure_ascii=False))
        return lines

    def _handle_turn_failed(self, data: Dict[str, Any]) -> List[str]:
        """Handle ``turn.failed`` / ``error`` — emit an error ``type: result``."""
        self._seen_turn_terminal = True

        lines = self._close_inflight_tools()

        error_msg = (
            data.get("error", {})
            if isinstance(data.get("error"), dict)
            else data.get("error", data.get("message", str(data)))
        )
        if isinstance(error_msg, dict):
            error_msg = error_msg.get("message", str(error_msg))

        result_event = {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "result": str(error_msg),
        }
        usage = self._usage_payload(data)
        if usage is not None:
            result_event["usage"] = usage
        cost_seen, cost = self._cost_payload(data)
        if cost_seen:
            result_event["total_cost_usd"] = cost
        self._apply_terminal_metadata(result_event, data)
        self._reset_turn_state()
        lines.append(json.dumps(result_event, ensure_ascii=False))
        return lines

    def finalize(self) -> List[str]:
        """Emit any trailing events after the codex process exits.

        If we accumulated agent messages but never saw a ``turn.completed``
        or ``turn.failed``, synthesize a ``type: result`` from whatever we
        have.  If nothing was accumulated, synthesize an error result so
        callers always get a terminal event.
        """
        if self._seen_turn_terminal:
            return []

        # Mark as terminal so a second call is a no-op
        self._seen_turn_terminal = True

        # A process that died mid-item leaves chips open just as a turn
        # terminal would, so the same closeout applies here.
        lines = self._close_inflight_tools()

        if self._agent_messages:
            result_event = {
                "type": "result",
                "result": self._result_text(),
            }
        else:
            # No output at all — synthesize an error result
            result_event = {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "result": "Codex process exited without producing output",
            }
        self._apply_terminal_metadata(result_event, {})
        self._reset_turn_state()
        lines.append(json.dumps(result_event, ensure_ascii=False))
        return lines


def _spawn_stdin_writer(proc: subprocess.Popen, payload: str) -> threading.Thread:
    """Write *payload* to ``proc.stdin`` in a daemon thread and close it.

    Same rationale as :func:`claude_runner._spawn_stdin_writer` — writing
    inline would deadlock once the kernel pipe buffer fills.
    """

    def _writer() -> None:
        try:
            assert proc.stdin is not None
            proc.stdin.write(payload)
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:
                pass

    t = threading.Thread(target=_writer, name="codex-stdin-writer", daemon=True)
    t.start()
    return t


def _spawn_stderr_reader(
    proc: subprocess.Popen,
    log_file: Optional[Path] = None,
    stderr_buffer: Optional[Deque[str]] = None,
) -> threading.Thread:
    """Drain ``proc.stderr`` in a daemon thread so the pipe never fills.

    Args:
        proc: The subprocess whose stderr to drain.
        log_file: Optional path to a sidecar stderr log file.
        stderr_buffer: Optional bounded deque (e.g. ``collections.deque(maxlen=100)``)
            to capture the tail of stderr lines for post-mortem analysis.
    """

    def _reader() -> None:
        log_fh = None
        if log_file is not None:
            try:
                log_file.parent.mkdir(parents=True, exist_ok=True)
                log_fh = open(log_file, "a", encoding="utf-8")
            except Exception:
                pass
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                # Capture into bounded buffer before any other processing
                if stderr_buffer is not None:
                    stderr_buffer.append(line)
                if log_fh is not None:
                    try:
                        log_fh.write(line)
                        log_fh.flush()
                    except Exception:
                        pass
                try:
                    print(line.rstrip("\n"), file=sys.stderr)
                except Exception:
                    pass
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            if log_fh is not None:
                try:
                    log_fh.close()
                except Exception:
                    pass

    t = threading.Thread(target=_reader, name="codex-stderr-reader", daemon=True)
    t.start()
    return t


class CodexRunner(AgentRunner):
    """OpenAI Codex CLI adapter — executes a single ``codex exec`` command.

    This runner wraps one specific codex CLI command (e.g. ``codex``).
    Agent selection/rotation is handled by :class:`LLMCaller`.

    Codex JSONL events are converted to Claude-compatible stream-json NDJSON
    by :class:`CodexEventConverter` so all upper-layer consumers
    (``StreamJSONTracker``, chat history, web console, etc.) work unchanged.
    """

    startup_provider = "openai"

    def __init__(
        self,
        project_root: Optional[Path] = None,
        command: Optional[Dict[str, Any]] = None,
    ):
        """Initialize with a single command.

        Args:
            project_root: Project root (unused for codex config loading,
                kept for interface compatibility).
            command: Single command dict ``{cmd, priority}``.
        """
        if command is not None:
            self.command = command
        else:
            self.command = {"cmd": "codex", "priority": 0}

    # ------------------------------------------------------------------
    # build_call_args — intent → CLI arguments
    # ------------------------------------------------------------------

    def build_call_args(
        self,
        prompt: str,
        read_only: bool,
        context_files: Optional[List[Path]] = None,
        invocation_intent: AgentInvocationIntent = AgentInvocationIntent.DEFAULT,
    ) -> List[str]:
        """Build codex CLI arguments from intent-level parameters.

        Produces the argv for ``codex exec --json``:

        * Constant prefix: ``exec --json --skip-git-repo-check``
        * Read-only: ``--sandbox read-only``
        * Writable: ``--sandbox danger-full-access``
        * Context files: content inlined into the prompt (codex has no
          ``--file`` equivalent)
        * Prompt: positional argument at the end, or ``-`` (stdin marker)
          when the prompt exceeds ``_MAX_ARG_BYTES``

        Returns:
            CLI argument list (excluding the command name, which is
            prepended by the execution methods).
        """
        args: List[str] = [
            "exec",
            "--json",
            "--skip-git-repo-check",
        ]

        # Sandbox / approval flags
        if read_only:
            args.extend(["--sandbox", "read-only"])
        else:
            args.extend(["--sandbox", "danger-full-access"])

        # Context files — codex has no --file flag, so inline content into prompt
        effective_prompt = prompt
        if context_files:
            inline_parts: List[str] = []
            for f in context_files:
                if f.exists():
                    try:
                        content = f.read_text(encoding="utf-8")
                        inline_parts.append(
                            f"## File: {f}\n```\n{content}\n```"
                        )
                    except Exception:
                        logger.debug("Failed to read context file %s", f)
            if inline_parts:
                effective_prompt = "\n\n".join(inline_parts) + "\n\n" + prompt

        # Prompt — positional argument at the end, or '-' for stdin routing
        if len(effective_prompt.encode("utf-8")) > _MAX_ARG_BYTES:
            args.append("-")
            # Store the actual prompt so run_with_monitor can pipe it via stdin
            self._pending_stdin_prompt = effective_prompt
        else:
            args.append(effective_prompt)
            self._pending_stdin_prompt = None

        return args

    # ------------------------------------------------------------------
    # run — synchronous execution
    # ------------------------------------------------------------------

    def run(
        self,
        args: List[str],
        timeout: Optional[int] = None,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        on_retry: Optional[Callable[[int, str], Optional[List[str]]]] = None,
    ) -> subprocess.CompletedProcess:
        """Run codex synchronously.

        Args:
            args: Arguments (typically from ``build_call_args``).
            timeout: Timeout in seconds.
            cwd: Working directory.
            env: Environment variables.
            on_retry: Ignored (kept for interface compatibility).

        Returns:
            subprocess.CompletedProcess with codex output converted to
            Claude-compatible NDJSON.
        """
        cmd_name = self.command["cmd"]
        full_cmd = [cmd_name] + args

        run_env = env if env is not None else dict(os.environ)
        run_env.pop("CLAUDECODE", None)

        stdin_prompt = getattr(self, "_pending_stdin_prompt", None)

        try:
            result = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=run_env,
                input=stdin_prompt,
            )

            # Convert codex JSONL output to Claude NDJSON
            converter = CodexEventConverter(
                provider=self.command.get("provider") or "openai"
            )
            converted_lines: List[str] = []
            for line in (result.stdout or "").splitlines(keepends=True):
                converted_lines.extend(converter.convert_line(line))
            converted_lines.extend(converter.finalize())

            return subprocess.CompletedProcess(
                args=full_cmd,
                returncode=result.returncode,
                stdout="\n".join(converted_lines),
                stderr=result.stderr or "",
            )

        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                args=full_cmd, returncode=124, stdout="", stderr="timeout"
            )

    # ------------------------------------------------------------------
    # run_with_monitor — monitored execution with streaming
    # ------------------------------------------------------------------

    def run_with_monitor(
        self,
        args: List[str],
        log_file: Optional[Path] = None,
        wall_timeout: Optional[int] = None,
        inactivity_timeout: int = 1800,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_activity: Optional[Callable[[], None]] = None,
        on_confirm: Optional[
            Callable[[str, List[str], Callable[[], bool]], Optional[str]]
        ] = None,
    ) -> "MonitoredResult":
        """Run codex with activity-based monitoring.

        Streams codex's JSONL stdout through :class:`CodexEventConverter`
        to produce Claude-compatible NDJSON, which is then written to the
        output buffer, log file, and ``on_output`` callback.

        Args:
            args: Arguments (typically from ``build_call_args``).
            log_file: Optional path to write output log.
            wall_timeout: Maximum total runtime in seconds.
            inactivity_timeout: Seconds without output before hang detection.
            cwd: Working directory.
            env: Environment variables.
            on_output: Callback for each converted output line.
            on_activity: Callback for activity detection.
            on_confirm: Optional callback for CLI-subprocess confirmation
                prompts (interface compat; codex rarely emits these).

        Returns:
            MonitoredResult with exit code, output, and metadata.
        """
        start_time = time.time()
        cmd_name = self.command["cmd"]

        run_env = env if env is not None else dict(os.environ)
        run_env.pop("CLAUDECODE", None)

        try:
            full_cmd = [cmd_name] + args

            print(
                f"[codex-runner] Running command: '{cmd_name}'",
                file=sys.stderr,
            )

            stdin_prompt = getattr(self, "_pending_stdin_prompt", None)

            result = self._run_single_with_monitor(
                full_cmd=full_cmd,
                cmd_name=cmd_name,
                log_file=log_file,
                wall_timeout=wall_timeout,
                inactivity_timeout=inactivity_timeout,
                cwd=cwd,
                env=run_env,
                on_output=on_output,
                on_activity=on_activity,
                start_time=start_time,
                stdin_prompt=stdin_prompt,
            )

            output = f"=== Command: {cmd_name} ===\n{result.output}"

            if result.interrupted:
                return MonitoredResult(
                    returncode=result.returncode,
                    output=output,
                    cmd_used=cmd_name,
                    cmd_index=0,
                    was_retry=False,
                    interrupted=True,
                    stderr_tail=getattr(result, "stderr_tail", "") or "",
                )

            if result.success:
                print(
                    f"[codex-runner] Command '{cmd_name}' succeeded",
                    file=sys.stderr,
                )

            return MonitoredResult(
                returncode=result.returncode,
                output=output,
                cmd_used=cmd_name,
                cmd_index=0,
                was_retry=False,
                stderr_tail=getattr(result, "stderr_tail", "") or "",
            )

        except Exception as e:
            msg = f"[codex-runner] Error running command '{cmd_name}': {e}"
            print(msg, file=sys.stderr)
            return MonitoredResult(
                returncode=1,
                output=msg,
                cmd_used=cmd_name,
                cmd_index=0,
                was_retry=False,
            )

    def _run_single_with_monitor(
        self,
        full_cmd: List[str],
        cmd_name: str,
        log_file: Optional[Path],
        wall_timeout: Optional[int],
        inactivity_timeout: int,
        cwd: Optional[Path],
        env: Optional[Dict[str, str]],
        on_output: Optional[Callable[[str], None]],
        on_activity: Optional[Callable[[], None]],
        start_time: float,
        stdin_prompt: Optional[str] = None,
    ) -> "_SingleRunResult":
        """Run a single command with monitoring and event conversion."""

        # Check if command exists
        if not shutil.which(full_cmd[0]):
            msg = f"\n[codex-runner] Command '{cmd_name}' not found, skipping...\n"
            if log_file:
                log_file.parent.mkdir(parents=True, exist_ok=True)
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(msg)
            return _SingleRunResult(
                returncode=127,
                output=msg,
                success=False,
                should_retry=True,
            )

        # stdin handling
        if stdin_prompt is not None:
            stdin_arg = subprocess.PIPE
        else:
            stdin_arg = None if sys.stdin.isatty() else subprocess.DEVNULL

        proc = subprocess.Popen(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=stdin_arg,
            cwd=cwd,
            env=env,
            bufsize=1,
            universal_newlines=True,
        )

        if stdin_prompt is not None:
            _spawn_stdin_writer(proc, stdin_prompt)

        # Drain stderr in background, capturing tail into a bounded buffer
        # for post-mortem analysis (e.g. shell snapshot validation failures).
        _stderr_log = None
        if log_file is not None:
            _stderr_log = log_file.parent / f"{log_file.name}.stderr"
        _stderr_buffer: Deque[str] = collections.deque(maxlen=_STDERR_BUFFER_MAXLEN)
        _stderr_thread = _spawn_stderr_reader(proc, log_file=_stderr_log, stderr_buffer=_stderr_buffer)

        output_buffer: List[str] = []
        last_activity = time.time()
        log_fh = None
        converter = CodexEventConverter(
            provider=self.command.get("provider") or "openai"
        )

        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(log_file, "a", encoding="utf-8")
            log_fh.write(f"\n=== Starting: {' '.join(full_cmd)} ===\n")
            log_fh.flush()

        try:
            while proc.poll() is None:
                # Check wall timeout
                if wall_timeout and (time.time() - start_time) > wall_timeout:
                    proc.kill()
                    proc.wait()
                    msg = f"\n[codex-runner] Wall timeout ({wall_timeout}s) exceeded\n"
                    output_buffer.append(msg)
                    if log_fh:
                        log_fh.write(msg)
                        log_fh.flush()
                    return _SingleRunResult(
                        returncode=124,
                        output="".join(output_buffer),
                        success=False,
                        should_retry=True,
                    )

                # Check for output with timeout
                try:
                    ready, _, _ = select.select([proc.stdout], [], [], 1.0)
                except InterruptedError:
                    continue
                except Exception as e:
                    output_buffer.append(f"\n[codex-runner] select() error: {e}\n")
                    continue

                if ready:
                    try:
                        line = proc.stdout.readline()
                        if line:
                            last_activity = time.time()
                            # Convert codex JSONL → Claude NDJSON
                            converted = converter.convert_line(line)
                            for ndjson_line in converted:
                                output_buffer.append(ndjson_line + "\n")
                                if log_fh:
                                    log_fh.write(ndjson_line + "\n")
                                    log_fh.flush()
                            if on_output:
                                for ndjson_line in converted:
                                    on_output(ndjson_line + "\n")
                            if on_activity:
                                on_activity()
                    except Exception:
                        pass
                else:
                    # Inactivity / hang detection
                    inactive_time = time.time() - last_activity
                    if inactive_time > inactivity_timeout:
                        hang_confirmed = False

                        if psutil:
                            try:
                                p = psutil.Process(proc.pid)
                                cpu_percent = p.cpu_percent(interval=0.5)
                                mem_info = p.memory_info()
                                if cpu_percent > 80.0:
                                    msg = (
                                        f"\n[codex-runner] Hang detected - high CPU usage "
                                        f"({cpu_percent:.1f}%) without output for "
                                        f"{int(inactive_time)}s\n"
                                    )
                                    hang_confirmed = True
                                elif mem_info.rss > 1024 * 1024 * 1024:
                                    msg = (
                                        f"\n[codex-runner] Hang detected - excessive memory "
                                        f"usage ({mem_info.rss // (1024*1024)}MB) without "
                                        f"output for {int(inactive_time)}s\n"
                                    )
                                    hang_confirmed = True
                            except Exception:
                                pass

                        if not hang_confirmed:
                            msg = (
                                f"\n[codex-runner] Hang detected - inactivity timeout "
                                f"({inactivity_timeout}s) - no output for "
                                f"{int(inactive_time)}s\n"
                            )
                            hang_confirmed = True

                        if hang_confirmed:
                            try:
                                proc.kill()
                                proc.wait(timeout=10)
                            except Exception:
                                try:
                                    proc.terminate()
                                    proc.wait(timeout=5)
                                except Exception:
                                    pass

                            output_buffer.append(msg)
                            if log_fh:
                                log_fh.write(msg)
                                log_fh.flush()
                            return _SingleRunResult(
                                returncode=124,
                                output="".join(output_buffer),
                                success=False,
                                should_retry=True,
                            )

            # Process finished — read remaining output
            remaining = proc.stdout.read()
            if remaining:
                for line in remaining.splitlines(keepends=True):
                    converted = converter.convert_line(line)
                    for ndjson_line in converted:
                        output_buffer.append(ndjson_line + "\n")
                        if log_fh:
                            log_fh.write(ndjson_line + "\n")
                            log_fh.flush()

            # Collect stderr tail from the bounded buffer for post-mortem.
            # Join the stderr reader thread first so the buffer is fully
            # populated and not being mutated during iteration.
            _stderr_thread.join(timeout=5)
            stderr_tail = "".join(_stderr_buffer) if _stderr_buffer else ""

            # Finalize converter (synthesize result if missing).
            # If the converter has no agent messages AND no turn terminal
            # event AND stderr contains a shell snapshot validation failure
            # pattern, we override finalize's default behavior and synthesize
            # an error result that carries the original stderr context,
            # forcing success=False even if returncode==0.
            _has_agent_output = bool(converter._agent_messages)
            _has_turn_terminal = converter._seen_turn_terminal
            _is_shell_snapshot_failure = (
                not _has_agent_output
                and not _has_turn_terminal
                and _detect_shell_snapshot_failure(stderr_tail)
            )

            if _is_shell_snapshot_failure:
                # Mark terminal so finalize() becomes a no-op
                converter._seen_turn_terminal = True
                # Synthesize error result with stderr context
                stderr_excerpt = stderr_tail[-2000:] if len(stderr_tail) > 2000 else stderr_tail
                error_text = (
                    "[codex-runner] Shell snapshot validation failed — "
                    "the Codex CLI reported a shell snapshot error during "
                    "startup/environment initialization. Original stderr:\n"
                    + stderr_excerpt
                )
                error_result = {
                    "type": "result",
                    "subtype": "error",
                    "is_error": True,
                    "result": error_text,
                }
                converter._apply_terminal_metadata(error_result, {})
                output_buffer.append(json.dumps(error_result, ensure_ascii=False) + "\n")
                if log_fh:
                    log_fh.write(json.dumps(error_result, ensure_ascii=False) + "\n")
                    log_fh.flush()
            else:
                for ndjson_line in converter.finalize():
                    output_buffer.append(ndjson_line + "\n")
                    if log_fh:
                        log_fh.write(ndjson_line + "\n")
                        log_fh.flush()

            returncode = proc.returncode
            output = "".join(output_buffer)

            # Shell snapshot failure forces failure even with returncode==0
            if _is_shell_snapshot_failure:
                return _SingleRunResult(
                    returncode=returncode if returncode != 0 else 1,
                    output=output,
                    success=False,
                    should_retry=False,
                    stderr_tail=stderr_tail,
                )

            # Unusual exit codes with hang context
            if returncode in (1, 137, 143):
                if "timeout" in output.lower():
                    return _SingleRunResult(
                        returncode=returncode,
                        output=output,
                        success=False,
                        should_retry=True,
                        stderr_tail=stderr_tail,
                    )

            return _SingleRunResult(
                returncode=returncode,
                output=output,
                success=returncode == 0,
                should_retry=False,
                stderr_tail=stderr_tail,
            )

        except KeyboardInterrupt:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
            try:
                remaining = proc.stdout.read()
                if remaining:
                    for line in remaining.splitlines(keepends=True):
                        converted = converter.convert_line(line)
                        for ndjson_line in converted:
                            output_buffer.append(ndjson_line + "\n")
            except Exception:
                pass
            # Finalize even on interrupt
            for ndjson_line in converter.finalize():
                output_buffer.append(ndjson_line + "\n")
            # Join stderr reader before reading the buffer
            _stderr_thread.join(timeout=5)
            stderr_tail = "".join(_stderr_buffer) if _stderr_buffer else ""
            return _SingleRunResult(
                returncode=-2,
                output="".join(output_buffer),
                success=False,
                should_retry=False,
                interrupted=True,
                stderr_tail=stderr_tail,
            )

        finally:
            if log_fh:
                log_fh.close()
            if proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    try:
                        proc.kill()
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # detect_infra_error — infrastructure error classification
    # ------------------------------------------------------------------

    def detect_infra_error(
        self,
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> InfraErrorType:
        """Detect infrastructure errors from codex execution results.

        Classification (in priority order):
        * ``returncode == 0`` → ``NONE`` (success, even if output contains
          keywords — those are likely prompt echo)
        * ``returncode == 124`` → ``TIMEOUT``
        * Output tail contains usage-limit / rate-limit keywords →
          ``USAGE_LIMIT``
        * Output tail contains authentication failure keywords →
          ``USAGE_LIMIT`` (auth failures are credential-level rotation
          errors, mapped to the same rotation trigger)
        * stderr tail matches shell snapshot validation failure patterns →
          ``STARTUP_FAILURE`` (runner infrastructure / environment snapshot
          failure; checked AFTER USAGE_LIMIT/TIMEOUT to avoid preempting
          those existing classifications)
        * Otherwise → ``NONE``
        """
        if returncode == 0:
            return InfraErrorType.NONE

        if returncode == 124:
            return InfraErrorType.TIMEOUT

        # Scan output tail for keywords
        combined = (stdout or "") + (stderr or "")
        tail = combined[-3000:].lower()
        lines = combined.split("\n")
        last_lines = "\n".join(lines[-20:]).lower()

        for keyword in _USAGE_LIMIT_KEYWORDS:
            if keyword in tail or keyword in last_lines:
                return InfraErrorType.USAGE_LIMIT

        # Auth failures → credential-level rotation (same as usage limit)
        for keyword in _AUTH_FAILURE_KEYWORDS:
            if keyword in tail or keyword in last_lines:
                return InfraErrorType.USAGE_LIMIT

        # Shell snapshot validation failure → startup/infra failure.
        # Checked after USAGE_LIMIT/TIMEOUT so those existing classifications
        # are never preempted.
        if _detect_shell_snapshot_failure(stderr or ""):
            return InfraErrorType.STARTUP_FAILURE

        return InfraErrorType.NONE


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MonitoredResult:
    """Result from CodexRunner.run_with_monitor."""

    returncode: int
    output: str
    cmd_used: str
    cmd_index: int
    was_retry: bool
    interrupted: bool = False
    stderr_tail: str = ""

    @property
    def success(self) -> bool:
        return self.returncode == 0


@dataclass
class _SingleRunResult:
    """Internal result from a single command run."""

    returncode: int
    output: str
    success: bool
    should_retry: bool
    interrupted: bool = False
    stderr_tail: str = ""
