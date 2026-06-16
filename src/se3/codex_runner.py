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

from .agent_runner import AgentRunner, InfraErrorType

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

    def __init__(self) -> None:
        self._agent_messages: List[str] = []
        self._seen_turn_terminal: bool = False
        self._tool_counter: int = 0
        self._touched_files: set = set()

    @property
    def touched_files(self) -> set:
        """Set of file paths from ``file_change`` items."""
        return self._touched_files

    def _next_tool_id(self) -> str:
        self._tool_counter += 1
        return f"codex_tool_{self._tool_counter}"

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
            if event_type == "thread.started":
                return []

            if event_type == "turn.started":
                return []

            if event_type == "item.updated" or event_type == "item.completed":
                return self._handle_item_event(event_type, event)

            if event_type == "turn.completed":
                return self._handle_turn_completed(event.get("data", event))

            if event_type in ("turn.failed", "error"):
                return self._handle_turn_failed(event.get("data", event))

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

    def _handle_item_event(self, event_type: str, event: Dict[str, Any]) -> List[str]:
        """Handle ``item.updated`` / ``item.completed`` events.

        The actual codex ``exec --json`` schema nests items under the ``item``
        key with types ``agent_message``, ``command_execution``,
        ``file_change``, and ``mcp_tool_call``::

            {"type": "item.completed", "item": {"type": "agent_message", "text": "Hello"}}
            {"type": "item.completed", "item": {"type": "command_execution", "command": "ls", "output": "...", "exit_code": 0}}
            {"type": "item.completed", "item": {"type": "file_change", "path": "/tmp/x.py", "content": "x=1"}}
            {"type": "item.completed", "item": {"type": "mcp_tool_call", "name": "...", ...}}
        """
        results: List[str] = []

        # Codex items are nested under the "item" key; fall back to the whole
        # event for forward-compat with possible schema variants.
        item = event.get("item", event)
        item_type = item.get("type", "")

        if item_type == "agent_message":
            text = item.get("text", "")
            if text:
                self._agent_messages.append(text)
                assistant_event = {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": text}],
                    },
                }
                results.append(json.dumps(assistant_event, ensure_ascii=False))

        elif item_type == "command_execution":
            tool_use_id = item.get("call_id", item.get("id", self._next_tool_id()))
            command = item.get("command", "")
            output = item.get("output", "")
            exit_code = item.get("exit_code", 0)
            is_error = exit_code != 0 if isinstance(exit_code, int) else False

            # tool_use event
            tool_use_event = {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": "Bash",
                            "input": {"command": command},
                        }
                    ],
                },
            }
            results.append(json.dumps(tool_use_event, ensure_ascii=False))

            # tool_result event
            tool_result_event = {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": output,
                            "is_error": is_error,
                        }
                    ],
                },
            }
            results.append(json.dumps(tool_result_event, ensure_ascii=False))

        elif item_type == "file_change":
            tool_use_id = item.get("call_id", item.get("id", self._next_tool_id()))
            path = item.get("path", item.get("file_path", ""))
            content = item.get("content", "")
            change_type = item.get("change_type", "write")

            # Map to the closest Claude tool
            mapped_name = "Write" if change_type in ("write", "create") else "Edit"
            tool_input: Dict[str, Any] = {"file_path": path}
            if change_type in ("write", "create"):
                tool_input["content"] = content
            else:
                tool_input["new_string"] = content

            # Record touched file for dependency tracking
            if path:
                self._touched_files.add(path)

            tool_use_event = {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": mapped_name,
                            "input": tool_input,
                        }
                    ],
                },
            }
            results.append(json.dumps(tool_use_event, ensure_ascii=False))

            # Synthesize a successful tool_result for file changes
            tool_result_event = {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": f"File {change_type}: {path}",
                            "is_error": False,
                        }
                    ],
                },
            }
            results.append(json.dumps(tool_result_event, ensure_ascii=False))

        elif item_type == "mcp_tool_call":
            tool_use_id = item.get("call_id", item.get("id", self._next_tool_id()))
            tool_name = item.get("name", "unknown")
            arguments = item.get("arguments", item.get("input", {}))
            if isinstance(arguments, str):
                try:
                    tool_input = json.loads(arguments)
                except (json.JSONDecodeError, ValueError):
                    tool_input = {"raw": arguments}
            else:
                tool_input = arguments

            tool_use_event = {
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
            results.append(json.dumps(tool_use_event, ensure_ascii=False))

            # Check for embedded output in the mcp_tool_call item
            output = item.get("output", item.get("result", ""))
            if output:
                tool_result_event = {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "content": str(output),
                                "is_error": False,
                            }
                        ],
                    },
                }
                results.append(json.dumps(tool_result_event, ensure_ascii=False))

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

    def _handle_turn_completed(self, data: Dict[str, Any]) -> List[str]:
        """Handle ``turn.completed`` — emit a ``type: result`` event."""
        self._seen_turn_terminal = True

        # Collect accumulated agent messages as the result text
        result_text = "\n".join(self._agent_messages) if self._agent_messages else ""

        # Parse usage — codex may carry it at multiple nesting levels.
        # Priority: data.usage → data.message.usage → data.turn.usage
        # Defensive: a key present with null value must not crash .get();
        # mirror _handle_turn_failed's isinstance(candidate, dict) guard.
        usage_raw: Dict[str, Any] = {}
        for candidate in (
            data.get("usage"),
            data.get("message", {}).get("usage") if isinstance(data.get("message"), dict) else None,
            data.get("turn", {}).get("usage") if isinstance(data.get("turn"), dict) else None,
        ):
            if isinstance(candidate, dict) and candidate:
                usage_raw = candidate
                break

        usage = {
            "input_tokens": usage_raw.get("input_tokens", 0),
            "output_tokens": usage_raw.get("output_tokens", 0),
            "cache_creation_input_tokens": usage_raw.get("cache_creation_input_tokens", 0),
            "cache_read_input_tokens": usage_raw.get("cached_input_tokens",
                                                       usage_raw.get("cache_read_input_tokens", 0)),
        }

        # total_cost_usd — check top-level first, then nested turn/message.
        # Treat None as 0 so explicit null doesn't propagate.
        cost = data.get("total_cost_usd") or 0
        if not cost and isinstance(data.get("turn"), dict):
            cost = data["turn"].get("total_cost_usd") or 0
        if not cost and isinstance(data.get("message"), dict):
            cost = data["message"].get("total_cost_usd") or 0

        result_event = {
            "type": "result",
            "result": result_text,
            "usage": usage,
            "total_cost_usd": cost,
        }
        return [json.dumps(result_event, ensure_ascii=False)]

    def _handle_turn_failed(self, data: Dict[str, Any]) -> List[str]:
        """Handle ``turn.failed`` / ``error`` — emit an error ``type: result``."""
        self._seen_turn_terminal = True

        error_msg = (
            data.get("error", {})
            if isinstance(data.get("error"), dict)
            else data.get("error", data.get("message", str(data)))
        )
        if isinstance(error_msg, dict):
            error_msg = error_msg.get("message", str(error_msg))

        # Extract usage if the failure event carries it (same multi-form
        # extraction as _handle_turn_completed); default to zeros.
        usage_raw: Dict[str, Any] = {}
        for candidate in (
            data.get("usage"),
            data.get("message", {}).get("usage") if isinstance(data.get("message"), dict) else None,
            data.get("turn", {}).get("usage") if isinstance(data.get("turn"), dict) else None,
        ):
            if isinstance(candidate, dict) and candidate:
                usage_raw = candidate
                break

        usage = {
            "input_tokens": usage_raw.get("input_tokens", 0),
            "output_tokens": usage_raw.get("output_tokens", 0),
            "cache_creation_input_tokens": usage_raw.get("cache_creation_input_tokens", 0),
            "cache_read_input_tokens": usage_raw.get("cached_input_tokens",
                                                       usage_raw.get("cache_read_input_tokens", 0)),
        }

        result_event = {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "result": str(error_msg),
            "usage": usage,
            "total_cost_usd": 0,
        }
        return [json.dumps(result_event, ensure_ascii=False)]

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

        if self._agent_messages:
            result_text = "\n".join(self._agent_messages)
            result_event = {
                "type": "result",
                "result": result_text,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
                "total_cost_usd": 0,
            }
            return [json.dumps(result_event, ensure_ascii=False)]

        # No output at all — synthesize an error result
        result_event = {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "result": "Codex process exited without producing output",
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "total_cost_usd": 0,
        }
        return [json.dumps(result_event, ensure_ascii=False)]


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
        spec_guard_settings: Optional[Path] = None,
    ) -> List[str]:
        """Build codex CLI arguments from intent-level parameters.

        ``spec_guard_settings`` is accepted for interface parity but ignored:
        the spec-write PreToolUse hook is a Claude-CLI concept, and codex
        sandboxing is handled separately via ``--sandbox``.

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
            converter = CodexEventConverter()
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
        converter = CodexEventConverter()

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
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                    "total_cost_usd": 0,
                }
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
