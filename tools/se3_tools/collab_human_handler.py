"""Human Call interaction handler for SE3 Collab.

Provides interactive handling when all tasks are blocked waiting for human input.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from .collab_render import CollabRenderer
    from .collab_orchestrator import Task

# Import at module level to avoid runtime import issues
from .claude_runner import ClaudeRunner


@dataclass
class HumanCall:
    """Represents a pending human call."""
    id: str
    call_type: str
    priority: str
    title: str
    context: str
    created: datetime
    file_path: Path


class InteractiveHumanHandler:
    """Interactive handler for human calls during collaboration.

    When all tasks are blocked, this handler provides a full-screen interactive
    mode for the user to respond to human calls.
    """

    def __init__(self, project_root: Path, renderer: "CollabRenderer"):
        self.project_root = project_root
        self.human_calls_dir = project_root / "human-calls"
        self.renderer = renderer

    async def check_and_handle(self, tasks: List) -> bool:
        """Check for pending human calls and handle them if all tasks are blocked.

        Args:
            tasks: List of all tasks in the collaboration

        Returns:
            True if handling is complete and collaboration can continue,
            False if still waiting for external response
        """
        pending = self._get_pending_calls()

        if not pending:
            return True

        # Check if all tasks are blocked
        blocked_tasks = [t for t in tasks if t.status in ("blocked", "failed")]
        if len(blocked_tasks) < len(tasks):
            # Some tasks still running, don't interrupt
            return True

        # All tasks blocked - enter interactive mode
        return await self._interactive_mode(pending, tasks)

    def _get_pending_calls(self) -> List[HumanCall]:
        """Get all pending human calls."""
        calls = []

        if not self.human_calls_dir.exists():
            return calls

        for f in self.human_calls_dir.glob("*.md"):
            # Skip already responded calls
            if f.name.endswith(".responded.md"):
                continue

            content = f.read_text()
            call = self._parse_call(f.stem, content, f)
            if call:
                calls.append(call)

        # Sort by priority and creation time
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        calls.sort(key=lambda c: (priority_order.get(c.priority, 99), c.created))

        return calls

    def _parse_call(self, call_id: str, content: str, file_path: Path) -> HumanCall | None:
        """Parse a human call file."""
        # Parse frontmatter
        frontmatter_match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
        if not frontmatter_match:
            # No frontmatter, treat entire content as context
            return HumanCall(
                id=call_id,
                call_type="general",
                priority="medium",
                title=call_id,
                context=content,
                created=datetime.now(),
                file_path=file_path,
            )

        frontmatter = frontmatter_match.group(1)
        body = content[frontmatter_match.end():]

        # Parse YAML-like frontmatter
        data = {}
        for line in frontmatter.split('\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                data[key.strip()] = value.strip()

        # Parse timestamp
        created_str = data.get('created', '')
        try:
            created = datetime.fromisoformat(created_str.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            created = datetime.now()

        return HumanCall(
            id=data.get('id', call_id),
            call_type=data.get('type', 'general'),
            priority=data.get('priority', 'medium'),
            title=data.get('title', call_id),
            context=body.strip(),
            created=created,
            file_path=file_path,
        )

    async def _interactive_mode(self, calls: List[HumanCall], tasks: List) -> bool:
        """Enter full-screen interactive mode for handling calls."""
        # Clear screen
        print("\033[2J\033[H", end='')

        print("=" * 70)
        print("⚠️  ALL TASKS BLOCKED - Human Input Required")
        print("=" * 70)

        print(f"\n{len(calls)} pending human call(s):")

        for i, call in enumerate(calls, 1):
            waiting_time = self._format_waiting_time(call.created)
            print(f"\n{i}. [{call.priority.upper()}] {call.title}")
            print(f"   Type: {call.call_type}")
            print(f"   Waiting: {waiting_time}")
            preview = call.context[:150].replace('\n', ' ')
            if len(call.context) > 150:
                preview += "..."
            print(f"   Preview: {preview}")

        print("\n" + "=" * 70)
        print("Options for each call:")
        print("  [v] View full context")
        print("  [s] Suggest response (AI help)")
        print("  [r] Reply in $EDITOR")
        print("  [k] Skip this call")
        print("  [w] Wait for external response (exit interactive mode)")
        print("=" * 70)

        # Process each call
        for call in calls:
            choice = await self._get_input(f"\nCall '{call.title}'? [v/s/r/k/w]: ")
            if choice is None:
                # Handle Ctrl+D or EOF - treat as 'wait'
                print("\n(EOF received, waiting for external response)")
                return False

            choice = choice.strip().lower()

            if choice == 'v':
                await self._view_full_context(call)
                # Ask again after viewing
                choice = await self._get_input(f"Now choose [s/r/k/w]: ")
                if choice is None:
                    print("\n(Interrupted, waiting for external response)")
                    return False
                choice = choice.strip().lower()

            if choice == 's':
                await self._handle_suggest(call)
            elif choice == 'r':
                self._handle_reply(call)
            elif choice == 'k':
                self._skip_call(call)
            elif choice == 'w':
                return False  # Continue waiting

        return True

    async def _get_input(self, prompt: str) -> str | None:
        """Get input from user asynchronously without blocking the event loop.

        Returns None if input was interrupted (EOFError/KeyboardInterrupt).
        """
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, lambda: input(prompt))
        except (EOFError, KeyboardInterrupt):
            return None

    async def _view_full_context(self, call: HumanCall):
        """Display full context of a human call."""
        print("\n" + "=" * 70)
        print(f"Full context for: {call.title}")
        print("=" * 70)
        print(call.context)
        print("=" * 70)
        await self._get_input("\nPress Enter to continue...")

    async def _handle_suggest(self, call: HumanCall):
        """Generate AI suggestion for a human call."""
        print("\n💡 Generating suggestion...")

        suggestion = await self._generate_suggestion(call)

        print(f"\nAI Suggestion:\n{'-' * 50}")
        print(suggestion)
        print('-' * 50)

        use = await self._get_input("\nUse this? [y/n/edit]: ")
        if use is None:
            print("\n(Cancelled)")
            return

        use = use.strip().lower()

        if use == 'y':
            self._write_response(call, suggestion)
            print("✓ Response saved")
        elif use == 'edit':
            edited = self._open_editor(suggestion)
            self._write_response(call, edited)
            print("✓ Response saved")

    def _handle_reply(self, call: HumanCall):
        """Open editor for manual reply."""
        response = self._open_editor("")
        if response.strip():
            self._write_response(call, response)
            print("✓ Response saved")
        else:
            print("(empty response, not saved)")

    def _skip_call(self, call: HumanCall):
        """Mark a call as skipped."""
        skip_file = self.human_calls_dir / f"{call.id}.skipped.md"
        skip_file.write_text(f"""---
id: {call.id}
status: skipped
skipped_at: {datetime.now().isoformat()}
---

Skipped by user in interactive mode.
""")
        print(f"✓ Call {call.id} skipped")

    async def _generate_suggestion(self, call: HumanCall) -> str:
        """Generate AI suggestion for a human call response."""
        prompt = f"""You are helping respond to a human call in a software development context.

Call Type: {call.call_type}
Priority: {call.priority}
Title: {call.title}

Context:
{call.context}

Please provide a helpful, actionable response. Be concise but thorough.
Your response will be used directly, so write it as the actual response."""

        try:
            runner = ClaudeRunner(self.project_root)
            # Run synchronously in a thread pool to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: runner.run(
                    ["--dangerously-skip-permissions", "--print", "-p", prompt],
                    timeout=60,
                )
            )

            if result.returncode == 0:
                return result.stdout
        except Exception as e:
            # Log error for debugging but don't expose to user
            print(f"[collab-human-handler] Failed to generate suggestion: {e}", file=sys.stderr)

        return "Unable to generate suggestion. Please respond manually."

    def _open_editor(self, initial_text: str) -> str:
        """Open system editor for text input."""
        editor = os.environ.get("EDITOR", "vim")

        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write(initial_text)
            temp_path = f.name

        try:
            subprocess.run([editor, temp_path], check=False)
            return Path(temp_path).read_text()
        finally:
            Path(temp_path).unlink(missing_ok=True)

    def _write_response(self, call: HumanCall, response: str):
        """Write response to a human call file."""
        response_file = self.human_calls_dir / f"{call.id}.responded.md"
        response_file.write_text(f"""---
id: {call.id}
status: responded
responded_at: {datetime.now().isoformat()}
original_file: {call.file_path.name}
---

{response}
""")

    def _format_waiting_time(self, created: datetime) -> str:
        """Format the waiting time since creation."""
        delta = datetime.now() - created

        if delta.days > 0:
            return f"{delta.days}d {delta.seconds // 3600}h"
        elif delta.seconds >= 3600:
            return f"{delta.seconds // 3600}h {(delta.seconds % 3600) // 60}m"
        elif delta.seconds >= 60:
            return f"{delta.seconds // 60}m"
        else:
            return f"{delta.seconds}s"


class SimpleHumanHandler:
    """Simple non-interactive human call handler."""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.human_calls_dir = project_root / "human-calls"

    def get_pending_count(self) -> int:
        """Get count of pending human calls."""
        if not self.human_calls_dir.exists():
            return 0

        count = 0
        for f in self.human_calls_dir.glob("*.md"):
            if not f.name.endswith(".responded.md") and not f.name.endswith(".skipped.md"):
                count += 1

        return count

    def list_pending(self) -> list[dict]:
        """List all pending human calls."""
        if not self.human_calls_dir.exists():
            return []

        pending = []
        for f in self.human_calls_dir.glob("*.md"):
            if f.name.endswith(".responded.md") or f.name.endswith(".skipped.md"):
                continue

            content = f.read_text()
            pending.append({
                "id": f.stem,
                "preview": content[:100] + "..." if len(content) > 100 else content,
            })

        return pending
