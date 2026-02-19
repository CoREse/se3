"""Terminal rendering engine for SE3 Collab - using rich library.

Provides real-time UI for manager decisions, worker status, and stream-json output.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.syntax import Syntax


# Pre-compile ANSI pattern for efficiency
_ANSI_PATTERN = re.compile(r'\x1B(?:[@-Z\-_]|\[[0-?]*[ -/]*[@-~])')


@dataclass
class WorkerStatus:
    """Represents the status of a worker task."""
    id: str
    status: str  # pending, running, done, failed, blocked
    progress: int = 0
    eta: str = "--"
    title: str = ""
    output_lines: deque[str] = field(default_factory=lambda: deque(maxlen=100))


class CollabRenderer:
    """Rich-based terminal renderer for collaborative sessions.

    Creates a three-panel layout:
    - Manager panel (top): Shows manager decisions and reasoning
    - Workers status (bottom-left): Table of all workers and their status
    - Output panel (bottom-right): Real-time output from active workers
    """

    def __init__(self, max_output_lines: int = 50):
        self.console = Console()
        self.layout = self._create_layout()
        self.workers: dict[str, WorkerStatus] = {}
        self.manager_lines: deque[str] = deque(maxlen=20)
        self.output_buffer: deque[str] = deque(maxlen=max_output_lines)
        self._live: Live | None = None

    def _create_layout(self) -> Layout:
        """Create the three-panel layout."""
        layout = Layout(name="root")

        # Split into manager (top) and workers (bottom)
        layout.split_column(
            Layout(name="manager", size=12),
            Layout(name="workers"),
        )

        # Split workers into status table and output
        layout["workers"].split_row(
            Layout(name="status", size=45),
            Layout(name="output"),
        )

        return layout

    def start_live(self) -> Live:
        """Start the live display.

        Returns a Live context manager that can be used with 'with' statement.
        """
        # Stop any existing live display first to prevent resource leaks
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass  # Ignore errors from stopping old display
            finally:
                self._live = None

        self._live = Live(
            self.layout,
            console=self.console,
            refresh_per_second=4,
            screen=False,
        )

        # Wrap the Live context manager to clear self._live on exit
        original_exit = self._live.__exit__

        def wrapped_exit(exc_type, exc_val, exc_tb):
            try:
                return original_exit(exc_type, exc_val, exc_tb)
            finally:
                self._live = None

        self._live.__exit__ = wrapped_exit
        return self._live

    def __enter__(self) -> "CollabRenderer":
        """Enter context manager - starts live display."""
        self.start_live()
        if self._live:
            self._live.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Exit context manager - stops live display.

        Returns:
            False to propagate exceptions, True to suppress them.
        """
        # Store exception info before cleanup to ensure it's preserved
        had_exception = exc_type is not None

        try:
            if self._live:
                self._live.stop()
        except Exception:
            # Log but don't suppress cleanup errors
            pass
        finally:
            # Always clear the live reference
            self._live = None

        # Always propagate exceptions (don't suppress)
        return False

    def stop_live(self):
        """Stop the live display."""
        if self._live:
            self._live.stop()
            self._live = None

    def reset(self):
        """Reset the renderer state for a new session.

        Clears workers, output buffers, and manager history.
        Should be called between iterations in loop mode.
        """
        self.workers.clear()
        self.manager_lines.clear()
        self.output_buffer.clear()
        # Recreate layout to ensure clean state
        self.layout = self._create_layout()

    def update_manager(self, text: str | list[str]):
        """Update the Manager decision panel.

        Args:
            text: Either a string or list of strings to display
        """
        if isinstance(text, list):
            content = "\n".join(text)
            self.manager_lines.extend(text)
        else:
            content = text
            # Add to history, keeping only last 20 lines
            for line in text.split("\n"):
                self.manager_lines.append(line)

        # Truncate to fit panel
        display_text = "\n".join(self.manager_lines[-20:])

        self.layout["manager"].update(
            Panel(
                display_text,
                title="[blue]Manager[/blue]",
                border_style="blue",
                subtitle="Planning & Decisions",
            )
        )

    def update_manager_decision(self, decision: dict[str, Any]):
        """Update manager panel with a structured decision."""
        action = decision.get("action", "unknown")
        reason = decision.get("reason", "")
        summary = decision.get("summary", "")

        action_colors = {
            "plan": "blue",
            "merge": "green",
            "reject": "red",
            "retry": "yellow",
            "split": "cyan",
            "escalate": "magenta",
            "complete": "green",
        }

        color = action_colors.get(action, "white")

        lines = [
            f"[{color}]Action: {action.upper()}[/{color}]",
        ]
        if summary:
            lines.append(f"Summary: {summary}")
        if reason:
            lines.append(f"Reason: {reason}")

        tasks = decision.get("tasks", [])
        if tasks:
            lines.append(f"\nTasks planned: {len(tasks)}")
            for task in tasks[:5]:  # Show first 5
                lines.append(f"  • {task.get('id', 'unknown')}: {task.get('title', '')[:40]}")
            if len(tasks) > 5:
                lines.append(f"  ... and {len(tasks) - 5} more")

        self.update_manager(lines)

    def add_worker(self, worker_id: str, title: str = ""):
        """Register a new worker."""
        self.workers[worker_id] = WorkerStatus(
            id=worker_id,
            status="pending",
            title=title,
        )
        self._update_workers_panel()

    def update_worker_status(
        self,
        worker_id: str,
        status: str | None = None,
        progress: int | None = None,
        eta: str | None = None,
    ):
        """Update a worker's status.

        Args:
            worker_id: The worker's ID
            status: Status value - must be one of: pending, running, done, failed, blocked, escalated
            progress: Progress percentage (0-100)
            eta: Estimated time to completion or retry info
        """
        if worker_id not in self.workers:
            self.add_worker(worker_id)

        worker = self.workers[worker_id]
        if status is not None:
            # Validate status to ensure it's a known state
            valid_statuses = {"pending", "running", "done", "failed", "blocked", "escalated"}
            if status not in valid_statuses:
                # If unknown status, append to output but don't change status
                self.append_worker_output(worker_id, f"[Status: {status}]")
            else:
                worker.status = status
        if progress is not None:
            worker.progress = progress
        if eta is not None:
            worker.eta = eta

        self._update_workers_panel()

    def append_worker_output(self, worker_id: str, line: str):
        """Append output from a worker."""
        if worker_id not in self.workers:
            self.add_worker(worker_id)

        # Store in worker's buffer
        self.workers[worker_id].output_lines.append(line)

        # Also add to global output buffer with worker prefix
        prefix = f"[{worker_id}] "
        self.output_buffer.append(prefix + line.rstrip())

        self._update_output_panel()

    def _update_workers_panel(self):
        """Update the workers status table."""
        table = Table(
            title="[bold]Workers[/bold]",
            show_header=True,
            header_style="bold magenta",
            expand=True,
        )

        table.add_column("ID", style="cyan", width=12)
        table.add_column("Status", width=10)
        table.add_column("Progress", width=10)
        table.add_column("ETA", width=8)
        table.add_column("Title", style="dim", overflow="fold")

        status_colors = {
            "pending": "yellow",
            "running": "blue",
            "done": "green",
            "failed": "red",
            "blocked": "red",
            "escalated": "magenta",
        }

        for worker in self.workers.values():
            color = status_colors.get(worker.status, "white")
            progress_bar = self._render_progress_bar(worker.progress)

            title = worker.title[:30] + "..." if len(worker.title) > 30 else worker.title

            table.add_row(
                worker.id,
                f"[{color}]{worker.status}[/{color}]",
                progress_bar,
                worker.eta,
                title,
            )

        self.layout["status"].update(
            Panel(table, border_style="green", title="[green]Status[/green]")
        )

    def _render_progress_bar(self, progress: int, width: int = 8) -> str:
        """Render a simple progress bar."""
        # Clamp progress to valid range
        progress = max(0, min(100, progress))
        filled = int(progress / 100 * width)
        bar = "█" * filled + "░" * (width - filled)
        return f"{bar} {progress}%"

    def _update_output_panel(self):
        """Update the output panel with recent lines."""
        # Get last 30 lines from buffer
        lines = list(self.output_buffer)[-30:]

        # Truncate long lines (accounting for potential ANSI codes)
        truncated = []
        for line in lines:
            # Strip ANSI escape sequences for length calculation
            # but keep them in the output for styling
            clean_line = self._strip_ansi(line)
            if len(clean_line) > 120:
                # Find safe truncation point (avoid cutting multi-byte chars)
                trunc_point = self._safe_truncate_point(line, 117)
                line = line[:trunc_point] + "..."
            truncated.append(line)

        content = "\n".join(truncated) if truncated else "(waiting for output...)"

        self.layout["output"].update(
            Panel(
                content,
                border_style="yellow",
                title="[yellow]Output[/yellow]",
            )
        )

    def _strip_ansi(self, text: str) -> str:
        """Strip ANSI escape sequences from text."""
        return _ANSI_PATTERN.sub('', text)

    def _safe_truncate_point(self, text: str, max_len: int) -> int:
        """Find a safe truncation point that doesn't split multi-byte characters."""
        if len(text) <= max_len:
            return len(text)

        # In Python 3, strings are Unicode code points. We need to find a valid
        # truncation point that doesn't split a grapheme cluster.
        # For simplicity, we truncate at max_len and let Python handle encoding
        # The key is to avoid splitting in the middle of a surrogate pair or
        # combining character sequence.

        # Try to find a good breaking point (space, punctuation, etc.)
        for i in range(min(max_len, len(text)), max(0, max_len - 20), -1):
            if i < len(text):
                # Check if this is a safe character to break after
                char = text[i - 1] if i > 0 else ''
                # Safe breaking characters
                if char in ' \t\n.,;:!?-_)]}>"\'':
                    return i

        # If no good breaking point found, just truncate at max_len
        # Python strings handle Unicode correctly at the code point level
        return min(max_len, len(text))

    def render_stream_json(self, line: str) -> dict[str, Any] | None:
        """Parse and render a stream-json line.

        Returns the parsed message if it was valid JSON, None otherwise.
        Also handles unknown message types by returning the parsed JSON for
        potential downstream processing.
        """
        # Skip empty lines
        if not line or not line.strip():
            return None

        try:
            msg = json.loads(line)
            msg_type = msg.get("type", "")

            if msg_type == "assistant":
                self._render_assistant_message(msg)
            elif msg_type == "user":
                # Handle tool results inside user messages
                self._render_user_message(msg)
            elif msg_type == "tool_use":
                self._render_tool_use(msg)
            elif msg_type == "tool_result":
                self._render_tool_result(msg)
            elif msg_type == "error":
                self._render_error(msg)
            elif msg_type == "result":
                self._render_result(msg)
            elif msg_type == "system":
                # Handle system messages (init, heartbeat, etc.)
                self._render_system_message(msg)
            elif msg_type == "thinking":
                # Handle thinking messages
                self._render_thinking_message(msg)
            else:
                # Unknown message type - add to output for visibility
                # but still return the parsed message
                subtype = msg.get("subtype", "unknown")
                self.output_buffer.append(f"[stream-json:{msg_type}/{subtype}] {str(msg.get('content', msg))[:80]}")
                self._update_output_panel()

            return msg
        except json.JSONDecodeError:
            # Not JSON, treat as plain text - add to output buffer
            stripped = line.rstrip()
            if stripped:
                self.output_buffer.append(stripped)
                self._update_output_panel()
            return None
        except Exception as e:
            # Handle any other unexpected errors gracefully
            self.output_buffer.append(f"[parse error] {str(e)[:80]}")
            self._update_output_panel()
            return None

    def _render_system_message(self, msg: dict[str, Any]):
        """Render a system message."""
        subtype = msg.get("subtype", "")
        if subtype == "init":
            self.output_buffer.append("[system] Session initialized")
            self._update_output_panel()
        elif subtype == "heartbeat":
            # Heartbeat messages are too noisy, skip them
            pass
        else:
            content = msg.get("content", "")
            if content:
                self.output_buffer.append(f"[system:{subtype}] {str(content)[:80]}")
                self._update_output_panel()

    def _render_thinking_message(self, msg: dict[str, Any]):
        """Render a thinking message."""
        thinking = msg.get("thinking", "")
        if thinking and len(thinking) > 10:  # Only show substantial thinking
            # Truncate very long thinking messages
            preview = str(thinking)[:100]
            if len(thinking) > 100:
                preview += "..."
            self.output_buffer.append(f"[thinking] {preview}")
            self._update_output_panel()

    def _render_assistant_message(self, msg: dict[str, Any]):
        """Render an assistant message from stream-json."""
        message = msg.get("message", {})
        content = message.get("content", [])

        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type", "")
                if item_type == "text":
                    text = item.get("text", "")
                    if text.strip():
                        self.output_buffer.append(f"[assistant] {text[:100]}")
                        self._update_output_panel()
                elif item_type == "tool_use":
                    # Handle tool_use inside assistant message
                    self._render_tool_use(item)

    def _render_tool_use(self, msg: dict[str, Any]):
        """Render a tool_use message."""
        name = msg.get("name", "unknown")
        input_data = msg.get("input", {})

        # Skip rendering certain noisy tools
        noisy_tools = {"Read", "Grep", "Glob"}
        if name in noisy_tools:
            # Still show the tool name but with less detail
            self.output_buffer.append(f"[tool] 📖 {name} (details hidden)")
            self._update_output_panel()
            return

        self.output_buffer.append(f"[tool] 🔧 {name}")

        # Show key parameters (limited to first 3, truncated)
        if isinstance(input_data, dict):
            for key, value in list(input_data.items())[:3]:
                preview = str(value)[:80]
                if len(str(value)) > 80:
                    preview += "..."
                self.output_buffer.append(f"[tool]   {key}: {preview}")

        self._update_output_panel()

    def _render_user_message(self, msg: dict[str, Any]):
        """Render a user message (contains tool results)."""
        message = msg.get("message", {})
        content = message.get("content", [])

        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                self._render_tool_result(item)

    def _render_tool_result(self, msg: dict[str, Any]):
        """Render a tool_result message."""
        result = msg.get("result", {})
        error = result.get("error") if isinstance(result, dict) else None

        if error:
            self.output_buffer.append(f"[result] ❌ Error: {str(error)[:80]}")
        else:
            output = result.get("output", "")
            if output:
                self.output_buffer.append(f"[result] ✓ {str(output)[:80]}")
            else:
                self.output_buffer.append("[result] ✓ Done")

        self._update_output_panel()

    def _render_result(self, msg: dict[str, Any]):
        """Render a final result message."""
        result = msg.get("result", "")
        if result:
            self.output_buffer.append(f"[final] {str(result)[:100]}")
            self._update_output_panel()

    def _render_error(self, msg: dict[str, Any]):
        """Render an error message."""
        error = msg.get("error", "Unknown error")
        self.output_buffer.append(f"[error] ❌ {str(error)[:100]}")
        self._update_output_panel()

    def print_message(self, message: str, style: str = ""):
        """Print a message outside the live display."""
        if self._live:
            try:
                self._live.stop()
                self.console.print(message, style=style)
                self._live.start()
            except Exception:
                # If live display fails, fall back to direct console output
                # and mark live as stopped to prevent further errors
                self._live = None
                self.console.print(message, style=style)
        else:
            self.console.print(message, style=style)

    def print_final_summary(self, success: bool, completed: int, failed: int):
        """Print a final summary after the live display ends."""
        self.stop_live()

        print("\n" + "=" * 60)
        if success:
            print("✅ Collaboration completed successfully")
        else:
            print("❌ Collaboration completed with issues")

        print(f"  Completed tasks: {completed}")
        print(f"  Failed tasks: {failed}")
        print("=" * 60 + "\n")


class SimpleRenderer:
    """Simple non-live renderer for basic output."""

    def __init__(self):
        self.console = Console()

    def update_manager(self, text: str):
        """Print manager update."""
        self.console.print(f"[Manager] {text}")

    def update_worker_status(self, worker_id: str, status: str):
        """Print worker status update."""
        self.console.print(f"[Worker {worker_id}] Status: {status}")

    def append_worker_output(self, worker_id: str, line: str):
        """Print worker output."""
        self.console.print(f"[{worker_id}] {line.rstrip()}")

    def render_stream_json(self, line: str) -> dict | None:
        """Parse stream-json line (no rendering)."""
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None
