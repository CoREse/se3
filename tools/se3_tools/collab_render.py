"""Terminal rendering engine for SE3 Collab - using rich library.

Provides real-time UI for manager decisions, worker status, and stream-json output.
"""

from __future__ import annotations

import json
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
        """Start the live display."""
        self._live = Live(
            self.layout,
            console=self.console,
            refresh_per_second=4,
            screen=False,
        )
        return self._live

    def stop_live(self):
        """Stop the live display."""
        if self._live:
            self._live.stop()
            self._live = None

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
        """Update a worker's status."""
        if worker_id not in self.workers:
            self.add_worker(worker_id)

        worker = self.workers[worker_id]
        if status is not None:
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

        # Truncate long lines
        truncated = []
        for line in lines:
            if len(line) > 120:
                line = line[:117] + "..."
            truncated.append(line)

        content = "\n".join(truncated) if truncated else "(waiting for output...)"

        self.layout["output"].update(
            Panel(
                content,
                border_style="yellow",
                title="[yellow]Output[/yellow]",
            )
        )

    def render_stream_json(self, line: str) -> dict[str, Any] | None:
        """Parse and render a stream-json line.

        Returns the parsed message if it was valid JSON, None otherwise.
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

            return msg
        except json.JSONDecodeError:
            # Not JSON, treat as plain text - add to output buffer
            stripped = line.rstrip()
            if stripped:
                self.output_buffer.append(stripped)
                self._update_output_panel()
            return None

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

    def _render_tool_use(self, msg: dict[str, Any]):
        """Render a tool_use message."""
        name = msg.get("name", "unknown")
        self.output_buffer.append(f"[tool] 🔧 {name}")
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

    def print_message(self, message: str, style: str = ""):
        """Print a message outside the live display."""
        if self._live:
            self._live.stop()
            self.console.print(message, style=style)
            self._live.start()
        else:
            self.console.print(message, style=style)


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
