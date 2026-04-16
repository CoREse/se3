"""SyncInteractionHandler — Concurrent dual-path human decision collection.

Provides two equivalent paths for humans to resolve pending sync decisions:
  Path A: Terminal interactive UI (Rich-rendered list, stdin input)
  Path B: File polling (MCP call file in se3/calls/, 1-second polling)

Either path completing first satisfies the request; the other is stopped.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import select
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_THREAD_JOIN_TIMEOUT = 5

class SyncInteractionHandler:
    """Collect human decisions for pending sync items via dual concurrent paths.

    Args:
        project_root: Project root directory (for se3/calls/ path).
        pending_items: List of PendingDecision dicts/objects to resolve.
    """

    def __init__(
        self,
        project_root: Path,
        pending_items: Optional[List[Any]] = None,
        use_terminal: Optional[bool] = None,
    ):
        self.project_root = project_root
        self._pending_items: List[Any] = pending_items or []
        self._use_terminal = use_terminal if use_terminal is not None else sys.stdin.isatty()

        self._decisions: Dict[str, str] = {}
        self._done_event = threading.Event()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._call_file_path: Optional[Path] = None

    def collect_decisions(
        self, pending_items: Optional[List[Any]] = None
    ) -> Dict[str, str]:
        """Collect decisions for all pending items via dual concurrent paths.

        When stdin is a TTY, starts both terminal interaction and file polling
        threads.  When stdin is not a TTY (e.g. piped or in a CI environment),
        only the file-polling path is started — the process blocks until the
        response file is written.

        Args:
            pending_items: Override the pending items list.

        Returns:
            Dict mapping item_id -> decision ('update_spec' or 'create_issue').
        """
        if pending_items is not None:
            self._pending_items = pending_items

        if not self._pending_items:
            return {}

        self._decisions = {}
        self._done_event.clear()
        self._stop_event.clear()

        call_file = self.generate_pending_call_file()
        self._call_file_path = call_file

        response_path = Path(str(call_file) + ".response")
        initial_hash: Optional[str] = None
        try:
            if response_path.exists():
                existing = response_path.read_text(encoding="utf-8")
                initial_hash = hashlib.sha256(existing.encode("utf-8")).hexdigest()
        except OSError:
            pass

        threads: list[threading.Thread] = []

        if self._use_terminal:
            terminal_thread = threading.Thread(
                target=self._terminal_path, name="sync-terminal", daemon=True
            )
            threads.append(terminal_thread)

        poll_thread = threading.Thread(
            target=self._file_watch_path,
            args=(call_file,),
            kwargs={"initial_hash": initial_hash},
            name="sync-poll",
            daemon=True,
        )
        threads.append(poll_thread)

        if not self._use_terminal:
            logger.info(
                "stdin is not a TTY — waiting for response file: %s.response",
                call_file,
            )

        for t in threads:
            t.start()

        try:
            while not self._done_event.is_set():
                self._done_event.wait(timeout=0.5)
        except KeyboardInterrupt:
            logger.info("Sync interaction interrupted by user")
            self._stop_event.set()
            for t in threads:
                t.join(timeout=_THREAD_JOIN_TIMEOUT)
            raise

        self._stop_event.set()
        for t in threads:
            t.join(timeout=_THREAD_JOIN_TIMEOUT)

        return dict(self._decisions)

    # ------------------------------------------------------------------
    # Path A: Terminal interactive UI
    # ------------------------------------------------------------------

    def _terminal_path(self) -> None:
        """Path A: Render pending items and collect decisions via stdin."""
        try:
            self._render_pending_items()
            decisions = self._collect_terminal_input()
            if decisions is None:
                return

            with self._lock:
                if not self._done_event.is_set():
                    self._decisions = decisions
                    self._write_response_file(decisions)
                    self._done_event.set()
        except Exception:
            if not self._stop_event.is_set():
                logger.debug("Terminal path error", exc_info=True)

    def _render_pending_items(self) -> None:
        """Display pending items using Rich."""
        from rich.console import Console
        from rich.table import Table
        from rich.panel import Panel

        console = Console()
        table = Table(title="Pending Decisions", expand=True, show_lines=True)
        table.add_column("#", style="bold cyan", width=4)
        table.add_column("Type", width=10)
        table.add_column("Spec", style="bold")
        table.add_column("Description")

        for idx, item in enumerate(self._pending_items, 1):
            item_type = self._get_field(item, "type", "?")
            spec_name = self._get_field(item, "spec_name", "?")
            description = self._get_field(item, "description", "")
            type_style = "red" if item_type == "conflict" else "yellow"
            table.add_row(
                str(idx),
                f"[{type_style}]{item_type}[/{type_style}]",
                spec_name,
                description,
            )

        console.print(table)
        console.print(
            Panel(
                "[bold]Options:[/bold]\n"
                "  Enter number and decision:  [cyan]1:1[/cyan] (update_spec)  "
                "[cyan]1:2[/cyan] (create_issue)\n"
                "  Batch all:  [cyan]all:1[/cyan] (all update_spec)  "
                "[cyan]all:2[/cyan] (all create_issue)\n"
                "  When done:  [cyan]done[/cyan]\n\n"
                "[dim]Or edit the .response file in se3/calls/ from another terminal.[/dim]",
                title="Decision Input",
                border_style="blue",
            )
        )

    def _read_line_interruptible(self) -> Optional[str]:
        """Read a line from stdin, checking stop/done events periodically.

        Uses select() on real ttys so the thread can be interrupted when the
        file-watch path wins.  Falls back to blocking input() when stdin is
        redirected or mocked (e.g. in tests).

        Returns None if interrupted by stop/done events or EOF.
        """
        try:
            fileno = sys.stdin.fileno()
        except (AttributeError, ValueError, OSError):
            if self._stop_event.is_set() or self._done_event.is_set():
                return None
            try:
                return input("")
            except (EOFError, KeyboardInterrupt):
                return None

        while not self._stop_event.is_set() and not self._done_event.is_set():
            ready, _, _ = select.select([fileno], [], [], 0.5)
            if ready:
                line = sys.stdin.readline()
                if not line:
                    raise EOFError
                return line.rstrip("\n")
        return None

    def _collect_terminal_input(self) -> Optional[Dict[str, str]]:
        """Read decisions from stdin. Returns None if stopped."""
        decisions: Dict[str, str] = {}
        total = len(self._pending_items)
        decision_map = {"1": "update_spec", "2": "create_issue"}

        while not self._stop_event.is_set():
            if self._done_event.is_set():
                return None

            try:
                prompt = f"Decision ({len(decisions)}/{total} resolved)> "
                sys.stdout.write(prompt)
                sys.stdout.flush()
                line = self._read_line_interruptible()
                if line is None:
                    return None
                line = line.strip()
            except EOFError:
                return None

            if not line:
                continue

            if line.lower() == "done":
                remaining = total - len(decisions)
                if remaining > 0:
                    print(f"  {remaining} item(s) still unresolved. Defaulting to create_issue.")
                    for idx, item in enumerate(self._pending_items):
                        item_id = self._get_field(item, "item_id", str(idx))
                        if item_id not in decisions:
                            decisions[item_id] = "create_issue"
                return decisions

            if line.lower().startswith("all:"):
                val = line.split(":", 1)[1].strip()
                if val not in decision_map:
                    print(f"  Invalid decision '{val}'. Use 1 (update_spec) or 2 (create_issue).")
                    continue
                decision = decision_map[val]
                for idx, item in enumerate(self._pending_items):
                    item_id = self._get_field(item, "item_id", str(idx))
                    decisions[item_id] = decision
                print(f"  All {total} items set to {decision}.")
                return decisions

            if ":" in line:
                parts = line.split(":", 1)
                try:
                    num = int(parts[0].strip())
                except ValueError:
                    print(f"  Invalid format. Use '<number>:<1|2>' or 'all:<1|2>'.")
                    continue

                val = parts[1].strip()
                if val not in decision_map:
                    print(f"  Invalid decision '{val}'. Use 1 (update_spec) or 2 (create_issue).")
                    continue

                if num < 1 or num > total:
                    print(f"  Invalid item number {num}. Range: 1-{total}.")
                    continue

                item = self._pending_items[num - 1]
                item_id = self._get_field(item, "item_id", str(num - 1))
                decisions[item_id] = decision_map[val]
                print(f"  #{num} -> {decision_map[val]}")

                if len(decisions) == total:
                    return decisions
            else:
                print("  Invalid format. Use '<number>:<1|2>', 'all:<1|2>', or 'done'.")

        return None

    # ------------------------------------------------------------------
    # Path B: File polling
    # ------------------------------------------------------------------

    def _file_watch_path(
        self, call_file: Path, *, initial_hash: Optional[str] = None,
    ) -> None:
        """Path B: Poll for .response file creation/modification."""
        response_path = Path(str(call_file) + ".response")
        last_content_hash = initial_hash

        while not self._stop_event.is_set():
            if self._done_event.is_set():
                return

            try:
                if response_path.exists():
                    content = response_path.read_text(encoding="utf-8")
                    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    if content_hash != last_content_hash:
                        last_content_hash = content_hash
                        decisions = self._parse_response_file(response_path, content=content)
                        if decisions is not None:
                            with self._lock:
                                if not self._done_event.is_set():
                                    self._decisions = decisions
                                    self._done_event.set()
                            return
            except OSError:
                pass

            self._stop_event.wait(timeout=1.0)

    def generate_pending_call_file(self) -> Path:
        """Generate an MCP call file for all pending decisions."""
        calls_dir = self.project_root / "se3" / "calls"
        calls_dir.mkdir(parents=True, exist_ok=True)

        timestamp = int(datetime.now().timestamp())
        unique_id = uuid.uuid4().hex[:8]
        call_file = calls_dir / f"sync_pending_{timestamp}_{unique_id}.json"

        items = []
        for idx, item in enumerate(self._pending_items, 1):
            items.append({
                "id": idx,
                "item_id": self._get_field(item, "item_id", ""),
                "type": self._get_field(item, "type", "gap"),
                "spec_name": self._get_field(item, "spec_name", ""),
                "description": self._get_field(item, "description", ""),
                "diff": self._get_field(item, "diff", ""),
                "confidence": self._get_field(item, "confidence", ""),
                "options": ["update_spec", "create_issue"],
                "decision": "pending",
            })

        call_data = {
            "type": "sync_pending_decisions",
            "timestamp": timestamp,
            "items": items,
        }

        call_file.write_text(
            json.dumps(call_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        logger.info("Generated pending decisions call file: %s", call_file)
        return call_file

    def _parse_response_file(
        self, response_path: Path, *, content: Optional[str] = None,
    ) -> Optional[Dict[str, str]]:
        """Parse a .response file into a decisions dict.

        Expected format::

            {
              "items": [
                {"id": 1, "item_id": "gap_auth_abc12345", "decision": "update_spec"},
                ...
              ]
            }

        Args:
            response_path: Path to the response file.
            content: Pre-read file content. If provided, the file is not re-read.

        Returns None if parsing fails or file is incomplete.
        Unresolved items default to create_issue (matching terminal path behavior).
        """
        try:
            raw = content if content is not None else response_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, OSError):
            return None

        resp_items = data.get("items", [])
        if not resp_items:
            return None

        decisions: Dict[str, str] = {}
        id_to_item_id = {}
        expected_item_ids: set[str] = set()
        for idx, item in enumerate(self._pending_items, 1):
            item_id = self._get_field(item, "item_id", str(idx - 1))
            id_to_item_id[idx] = item_id
            expected_item_ids.add(item_id)

        for resp in resp_items:
            decision = resp.get("decision", "")
            if decision not in ("update_spec", "create_issue"):
                continue

            item_id = resp.get("item_id", "")
            if item_id:
                decisions[item_id] = decision
            else:
                num_id = resp.get("id")
                if num_id and num_id in id_to_item_id:
                    decisions[id_to_item_id[num_id]] = decision

        if not decisions:
            return None

        missing = expected_item_ids - decisions.keys()
        if missing:
            logger.debug(
                "Response file missing item_ids (defaulting to create_issue): %s",
                missing,
            )
            for item_id in missing:
                decisions[item_id] = "create_issue"

        return decisions

    def _write_response_file(self, decisions: Dict[str, str]) -> None:
        """Write decisions back to the .response file atomically.

        Uses write-to-temp-then-rename to prevent the file poll thread
        from reading a partially written file.
        """
        if self._call_file_path is None:
            return

        response_path = Path(str(self._call_file_path) + ".response")

        items = []
        for idx, item in enumerate(self._pending_items, 1):
            item_id = self._get_field(item, "item_id", str(idx - 1))
            items.append({
                "id": idx,
                "item_id": item_id,
                "decision": decisions.get(item_id, "create_issue"),
            })

        response_data = {"items": items}

        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(response_path.parent),
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(response_data, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, str(response_path))
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as e:
            logger.warning("Failed to write response file: %s", e)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_field(item: Any, field: str, default: str = "") -> str:
        """Get a field from a PendingDecision (dataclass or dict)."""
        if isinstance(item, dict):
            return str(item.get(field, default))
        return str(getattr(item, field, default))
