"""Structured logging configuration for SE3 flow engine.

Provides JSON-formatted logging with step tracking, timing, and LLM metrics.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class LogLevel(str, Enum):
    """Log levels for structured logging."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class LogEventType(str, Enum):
    """Types of log events in the flow engine."""

    # Flow lifecycle
    FLOW_STARTED = "flow_started"
    FLOW_COMPLETED = "flow_completed"
    FLOW_FAILED = "flow_failed"
    FLOW_PAUSED = "flow_paused"
    FLOW_RESUMED = "flow_resumed"

    # Step lifecycle
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    STEP_RETRY = "step_retry"

    # LLM operations
    LLM_CALL_STARTED = "llm_call_started"
    LLM_CALL_COMPLETED = "llm_call_completed"
    LLM_CALL_FAILED = "llm_call_failed"
    LLM_FALLBACK = "llm_fallback"

    # Persistence
    STATE_SAVED = "state_saved"
    STATE_LOADED = "state_loaded"
    CONTEXT_EXPORTED = "context_exported"

    # Tool operations
    TOOL_EXECUTION = "tool_execution"
    TEST_EXECUTION = "test_execution"
    GIT_OPERATION = "git_operation"


class StructuredLogEntry:
    """A single structured log entry.

    Attributes:
        timestamp: When the event occurred
        event_type: Type of event
        level: Log level
        flow_id: Associated flow ID
        step_id: Associated step ID (if applicable)
        step_type: Type of step (if applicable)
        message: Human-readable message
        data: Structured data payload
        duration_ms: Duration in milliseconds (if applicable)
        model: LLM model used (if applicable)
        tokens: Token usage info (if applicable)
    """

    def __init__(
        self,
        event_type: LogEventType,
        level: LogLevel = LogLevel.INFO,
        flow_id: Optional[str] = None,
        step_id: Optional[str] = None,
        step_type: Optional[str] = None,
        message: str = "",
        data: Optional[Dict[str, Any]] = None,
        duration_ms: Optional[float] = None,
        model: Optional[str] = None,
        tokens: Optional[Dict[str, int]] = None,
    ):
        self.timestamp = datetime.utcnow().isoformat() + "Z"
        self.event_type = event_type
        self.level = level
        self.flow_id = flow_id
        self.step_id = step_id
        self.step_type = step_type
        self.message = message
        self.data = data or {}
        self.duration_ms = duration_ms
        self.model = model
        self.tokens = tokens

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = {
            "timestamp": self.timestamp,
            "event_type": self.event_type.value,
            "level": self.level.value,
            "message": self.message,
        }

        if self.flow_id:
            result["flow_id"] = self.flow_id
        if self.step_id:
            result["step_id"] = self.step_id
        if self.step_type:
            result["step_type"] = self.step_type
        if self.data:
            result["data"] = self.data
        if self.duration_ms is not None:
            result["duration_ms"] = round(self.duration_ms, 2)
        if self.model:
            result["model"] = self.model
        if self.tokens:
            result["tokens"] = self.tokens

        return result

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), default=str)


class StructuredLogger:
    """Logger that outputs structured JSON logs.

    Logs are written to both a JSON lines file and optionally
    to stderr in human-readable format.
    """

    def __init__(
        self,
        project_root: Path,
        log_dir: Optional[Path] = None,
        console_output: bool = True,
        min_level: LogLevel = LogLevel.INFO,
    ):
        """Initialize structured logger.

        Args:
            project_root: Project root directory
            log_dir: Directory for log files (default: se3/logs)
            console_output: Whether to output to console
            min_level: Minimum log level to record
        """
        self.project_root = Path(project_root)
        self.log_dir = log_dir or (self.project_root / "se3" / "logs")
        self.console_output = console_output
        self.min_level = min_level

        # Ensure log directory exists
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Current log file (rotated daily)
        self.log_file = self._get_log_file()

        # In-memory buffer for recent logs (for status queries)
        self.recent_logs: List[StructuredLogEntry] = []
        self.max_buffer_size = 1000

        # Timing trackers
        self._step_timers: Dict[str, float] = {}
        self._llm_timers: Dict[str, float] = {}

    def _get_log_file(self) -> Path:
        """Get the log file for today."""
        date_str = datetime.now().strftime("%Y-%m-%d")
        return self.log_dir / f"engine-{date_str}.jsonl"

    def _should_log(self, level: LogLevel) -> bool:
        """Check if a log level should be recorded."""
        level_order = {
            LogLevel.DEBUG: 0,
            LogLevel.INFO: 1,
            LogLevel.WARNING: 2,
            LogLevel.ERROR: 3,
            LogLevel.CRITICAL: 4,
        }
        return level_order[level] >= level_order[self.min_level]

    def _write_log(self, entry: StructuredLogEntry) -> None:
        """Write log entry to file and buffer."""
        # Update log file if date changed
        current_file = self._get_log_file()

        # Append to JSON lines file
        with open(current_file, "a", encoding="utf-8") as f:
            f.write(entry.to_json() + "\n")

        # Add to buffer
        self.recent_logs.append(entry)
        if len(self.recent_logs) > self.max_buffer_size:
            self.recent_logs = self.recent_logs[-self.max_buffer_size:]

        # Console output
        if self.console_output:
            self._console_output(entry)

    def _console_output(self, entry: StructuredLogEntry) -> None:
        """Output log entry to console in human-readable format."""
        level_colors = {
            LogLevel.DEBUG: "\033[36m",  # Cyan
            LogLevel.INFO: "\033[0m",    # Default
            LogLevel.WARNING: "\033[33m", # Yellow
            LogLevel.ERROR: "\033[31m",   # Red
            LogLevel.CRITICAL: "\033[35m", # Magenta
        }
        reset = "\033[0m"

        color = level_colors.get(entry.level, "")
        prefix = f"[{entry.timestamp[:19]}] {entry.level.value.upper():8}"

        # Build context string
        context_parts = []
        if entry.flow_id:
            context_parts.append(f"flow={entry.flow_id}")
        if entry.step_type:
            context_parts.append(f"step={entry.step_type}")

        context = f" ({', '.join(context_parts)})" if context_parts else ""

        # Format message
        msg = entry.message
        if entry.duration_ms is not None:
            msg += f" [{entry.duration_ms:.0f}ms]"
        if entry.model:
            msg += f" [model={entry.model}]"

        print(f"{color}{prefix}{context}: {msg}{reset}", file=sys.stderr)

    def log(
        self,
        event_type: LogEventType,
        level: LogLevel = LogLevel.INFO,
        message: str = "",
        flow_id: Optional[str] = None,
        step_id: Optional[str] = None,
        step_type: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
        duration_ms: Optional[float] = None,
        model: Optional[str] = None,
        tokens: Optional[Dict[str, int]] = None,
    ) -> None:
        """Log a structured event.

        Args:
            event_type: Type of event
            level: Log level
            message: Human-readable message
            flow_id: Associated flow ID
            step_id: Associated step ID
            step_type: Type of step
            data: Structured data payload
            duration_ms: Duration in milliseconds
            model: LLM model used
            tokens: Token usage (input, output)
        """
        if not self._should_log(level):
            return

        entry = StructuredLogEntry(
            event_type=event_type,
            level=level,
            flow_id=flow_id,
            step_id=step_id,
            step_type=step_type,
            message=message,
            data=data,
            duration_ms=duration_ms,
            model=model,
            tokens=tokens,
        )
        self._write_log(entry)

    # Convenience methods for common events

    def flow_started(self, flow_id: str, task: str, task_type: str) -> None:
        """Log flow started event."""
        self.log(
            event_type=LogEventType.FLOW_STARTED,
            level=LogLevel.INFO,
            message=f"Flow started: {task[:60]}...",
            flow_id=flow_id,
            data={"task": task, "task_type": task_type},
        )

    def flow_completed(self, flow_id: str, duration_ms: float) -> None:
        """Log flow completed event."""
        self.log(
            event_type=LogEventType.FLOW_COMPLETED,
            level=LogLevel.INFO,
            message="Flow completed successfully",
            flow_id=flow_id,
            duration_ms=duration_ms,
        )

    def flow_failed(self, flow_id: str, error: str, duration_ms: Optional[float] = None) -> None:
        """Log flow failed event."""
        self.log(
            event_type=LogEventType.FLOW_FAILED,
            level=LogLevel.ERROR,
            message=f"Flow failed: {error}",
            flow_id=flow_id,
            data={"error": error},
            duration_ms=duration_ms,
        )

    def step_started(self, flow_id: str, step_id: str, step_type: str) -> None:
        """Log step started event."""
        self._step_timers[step_id] = time.time()
        self.log(
            event_type=LogEventType.STEP_STARTED,
            level=LogLevel.INFO,
            message=f"Step started: {step_type}",
            flow_id=flow_id,
            step_id=step_id,
            step_type=step_type,
        )

    def step_completed(
        self,
        flow_id: str,
        step_id: str,
        step_type: str,
        outputs: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log step completed event."""
        start_time = self._step_timers.pop(step_id, None)
        duration_ms = (time.time() - start_time) * 1000 if start_time else None

        self.log(
            event_type=LogEventType.STEP_COMPLETED,
            level=LogLevel.INFO,
            message=f"Step completed: {step_type}",
            flow_id=flow_id,
            step_id=step_id,
            step_type=step_type,
            duration_ms=duration_ms,
            data={"outputs": outputs} if outputs else None,
        )

    def step_failed(
        self,
        flow_id: str,
        step_id: str,
        step_type: str,
        error: str,
        will_retry: bool = False,
    ) -> None:
        """Log step failed event."""
        start_time = self._step_timers.pop(step_id, None)
        duration_ms = (time.time() - start_time) * 1000 if start_time else None

        self.log(
            event_type=LogEventType.STEP_FAILED,
            level=LogLevel.ERROR if not will_retry else LogLevel.WARNING,
            message=f"Step failed: {step_type}" + (" (will retry)" if will_retry else ""),
            flow_id=flow_id,
            step_id=step_id,
            step_type=step_type,
            duration_ms=duration_ms,
            data={"error": error, "will_retry": will_retry},
        )

    def llm_call_started(
        self,
        flow_id: str,
        step_id: str,
        model: str,
        prompt_size: Optional[int] = None,
    ) -> None:
        """Log LLM call started event."""
        timer_key = f"{flow_id}:{step_id}"
        self._llm_timers[timer_key] = time.time()

        self.log(
            event_type=LogEventType.LLM_CALL_STARTED,
            level=LogLevel.DEBUG,
            message=f"LLM call started with {model}",
            flow_id=flow_id,
            step_id=step_id,
            model=model,
            data={"prompt_size": prompt_size},
        )

    def llm_call_completed(
        self,
        flow_id: str,
        step_id: str,
        model: str,
        tokens_input: int,
        tokens_output: int,
    ) -> None:
        """Log LLM call completed event."""
        timer_key = f"{flow_id}:{step_id}"
        start_time = self._llm_timers.pop(timer_key, None)
        duration_ms = (time.time() - start_time) * 1000 if start_time else None

        self.log(
            event_type=LogEventType.LLM_CALL_COMPLETED,
            level=LogLevel.DEBUG,
            message=f"LLM call completed",
            flow_id=flow_id,
            step_id=step_id,
            model=model,
            duration_ms=duration_ms,
            tokens={"input": tokens_input, "output": tokens_output},
        )

    def llm_call_failed(
        self,
        flow_id: str,
        step_id: str,
        model: str,
        error: str,
    ) -> None:
        """Log LLM call failed event."""
        timer_key = f"{flow_id}:{step_id}"
        start_time = self._llm_timers.pop(timer_key, None)
        duration_ms = (time.time() - start_time) * 1000 if start_time else None

        self.log(
            event_type=LogEventType.LLM_CALL_FAILED,
            level=LogLevel.ERROR,
            message=f"LLM call failed with {model}: {error}",
            flow_id=flow_id,
            step_id=step_id,
            model=model,
            duration_ms=duration_ms,
            data={"error": error},
        )

    def state_saved(self, flow_id: str, state_file: Path) -> None:
        """Log state saved event."""
        self.log(
            event_type=LogEventType.STATE_SAVED,
            level=LogLevel.DEBUG,
            message=f"State saved to {state_file.name}",
            flow_id=flow_id,
            data={"state_file": str(state_file)},
        )

    def get_recent_logs(
        self,
        event_type: Optional[LogEventType] = None,
        flow_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[StructuredLogEntry]:
        """Get recent log entries from memory buffer.

        Args:
            event_type: Filter by event type
            flow_id: Filter by flow ID
            limit: Maximum number of entries to return

        Returns:
            List of matching log entries
        """
        filtered = self.recent_logs

        if event_type:
            filtered = [e for e in filtered if e.event_type == event_type]
        if flow_id:
            filtered = [e for e in filtered if e.flow_id == flow_id]

        return filtered[-limit:]

    def export_logs(
        self,
        output_file: Path,
        since: Optional[datetime] = None,
        event_type: Optional[LogEventType] = None,
    ) -> int:
        """Export logs to a JSON file.

        Args:
            output_file: Path to output file
            since: Only export logs since this time
            event_type: Filter by event type

        Returns:
            Number of entries exported
        """
        entries = []

        # Read all log files
        for log_file in sorted(self.log_dir.glob("engine-*.jsonl")):
            with open(log_file, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        data = json.loads(line)
                        if since:
                            log_time = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
                            if log_time < since:
                                continue
                        if event_type and data.get("event_type") != event_type.value:
                            continue
                        entries.append(data)
                    except (json.JSONDecodeError, KeyError):
                        continue

        # Write to output file
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, default=str)

        return len(entries)


# Global logger instance (lazy initialization)
_logger_instance: Optional[StructuredLogger] = None


def get_logger(project_root: Optional[Path] = None) -> StructuredLogger:
    """Get or create the global logger instance.

    Args:
        project_root: Project root (required on first call)

    Returns:
        StructuredLogger instance
    """
    global _logger_instance
    if _logger_instance is None:
        if project_root is None:
            raise RuntimeError("project_root required for first logger initialization")
        _logger_instance = StructuredLogger(project_root)
    return _logger_instance


def reset_logger() -> None:
    """Reset the global logger instance (for testing)."""
    global _logger_instance
    _logger_instance = None
