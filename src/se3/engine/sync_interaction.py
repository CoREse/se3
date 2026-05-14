"""SyncInteractionHandler — Concurrent dual-path approval for high-impact deletions.

In the one-directional sync model, the only spec change that ever requires a
human gate is a **high-impact deletion** — i.e. ``se3 sync`` is about to
remove an entire ``### Requirement:`` section because the code no longer
implements it. Everything else (description tweaks, new sections,
in-place rewrites) is auto-applied and trivially reversible by the next
sync round.

This handler offers two equivalent input paths:
  Path A: Terminal interactive UI (Rich-rendered list, stdin input)
  Path B: File polling (MCP call file in ``se3/calls/``, 1-second polling)

Whichever path completes first satisfies the request; the other is stopped.
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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_THREAD_JOIN_TIMEOUT = 5

_VALID_DECISIONS = {"approve", "skip"}


def prompt_resume_or_exit(stats: Dict[str, Any]) -> str:
    """Prompt the operator to continue or exit after sustained infra failures.

    Used by ``SyncLoop`` when LLM quota is exhausted or the
    infrastructure-failure threshold is hit. The prompt blocks the run
    until the user makes a decision; ``Ctrl-C`` raises
    ``KeyboardInterrupt`` (the loop catches it and persists the
    checkpoint).

    Args:
        stats: Dict shown to the user. Keys (all optional, all rendered
            verbatim if present): ``completed_specs``, ``total_specs``,
            ``round_index``, ``max_rounds``, ``in_sync_specs`` (iterable
            of spec names), ``failure_count``, ``checkpoint_path``,
            ``reason``.

    Returns:
        ``"continue"`` when the user accepts (Enter on a TTY), or
        ``"exit"`` when stdin is not a TTY (the caller is expected to
        treat that as a request to stop and resume later).
    """
    completed = stats.get("completed_specs")
    total = stats.get("total_specs")
    round_idx = stats.get("round_index")
    max_rounds = stats.get("max_rounds")
    in_sync_specs = stats.get("in_sync_specs") or []
    failure_count = stats.get("failure_count")
    checkpoint_path = stats.get("checkpoint_path")
    reason = stats.get("reason") or "infrastructure failure threshold reached"

    lines = []
    failure_summary = (
        f"detected {failure_count} consecutive infrastructure failures"
        if failure_count
        else "detected sustained infrastructure failures"
    )
    lines.append(f"⚠ Sync paused — {failure_summary} ({reason}).")
    if completed is not None and total is not None:
        lines.append(f"  Completed specs: {completed}/{total}")
    if round_idx is not None and max_rounds is not None:
        lines.append(f"  Current round: {round_idx}/{max_rounds}")
    if in_sync_specs:
        names = list(in_sync_specs)
        head = ", ".join(names[:8])
        tail = "" if len(names) <= 8 else f", … (+{len(names) - 8} more)"
        lines.append(f"  In-sync specs: {head}{tail}")
    if checkpoint_path is not None:
        lines.append(f"  Checkpoint written: {checkpoint_path}")

    message = "\n".join(lines)

    try:
        is_tty = sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        is_tty = False

    if not is_tty:
        sys.stderr.write(message + "\n")
        sys.stderr.write(
            "Non-interactive stdin — exiting. Re-run with "
            "`se3 sync --resume` once the quota / infrastructure recovers.\n"
        )
        sys.stderr.flush()
        return "exit"

    sys.stdout.write(message + "\n")
    sys.stdout.write(
        "Press Enter to continue once the quota recovers; press Ctrl-C "
        "to exit (resume later with `se3 sync --resume`).\n"
    )
    sys.stdout.flush()

    try:
        line = sys.stdin.readline()
    except KeyboardInterrupt:
        raise
    if line == "":
        # readline() returns "" only on EOF (e.g. Ctrl-D or closed stdin).
        # We cannot distinguish "user pressed Enter" from "stdin closed"
        # otherwise, so treat EOF as an explicit exit signal: the caller
        # persists the checkpoint and stops the loop instead of burning
        # more LLM calls against a no-op gate.
        sys.stderr.write(
            "stdin closed (EOF) — exiting. Re-run with "
            "`se3 sync --resume` once the quota / infrastructure recovers.\n"
        )
        sys.stderr.flush()
        return "exit"
    return "continue"


@dataclass
class HighImpactDeletion:
    """A pending whole-requirement removal awaiting human approval."""

    item_id: str
    spec_name: str
    requirement_name: str = ""
    requirement_excerpt: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "spec_name": self.spec_name,
            "requirement_name": self.requirement_name,
            "requirement_excerpt": self.requirement_excerpt,
        }


class SyncInteractionHandler:
    """Collect approve/skip decisions for pending high-impact deletions.

    Args:
        project_root: Project root directory (for ``se3/calls/`` path).
        pending_items: List of ``HighImpactDeletion`` to resolve.
        use_terminal: Force terminal path on/off. Defaults to ``stdin.isatty()``.
    """

    def __init__(
        self,
        project_root: Path,
        pending_items: Optional[List[HighImpactDeletion]] = None,
        use_terminal: Optional[bool] = None,
    ):
        self.project_root = project_root
        self._pending_items: List[HighImpactDeletion] = list(pending_items or [])
        self._use_terminal = (
            use_terminal if use_terminal is not None else sys.stdin.isatty()
        )

        self._decisions: Dict[str, str] = {}
        self._done_event = threading.Event()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._call_file_path: Optional[Path] = None

    def collect_decisions(
        self, pending_items: Optional[List[HighImpactDeletion]] = None
    ) -> Dict[str, str]:
        """Collect approve/skip decisions for all pending items.

        When stdin is a TTY, starts both terminal interaction and file-polling
        threads. When stdin is not a TTY (e.g. CI), only the file-polling
        path is started — the process blocks until the response file is
        written.

        Returns:
            Dict mapping ``item_id -> 'approve'|'skip'``.
        """
        if pending_items is not None:
            self._pending_items = list(pending_items)

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
        """Path A: render pending items and collect approve/skip from stdin."""
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
        from rich.table import Table

        from . import display

        console = display.get_console()
        table = Table(title="High-Impact Deletions", expand=True, show_lines=True)
        table.add_column("#", style="bold cyan", width=4)
        table.add_column("Spec", style="bold")
        table.add_column("Requirement")
        table.add_column("Excerpt")

        for idx, item in enumerate(self._pending_items, 1):
            table.add_row(
                str(idx),
                item.spec_name,
                item.requirement_name or "(unspecified)",
                (item.requirement_excerpt or "")[:120],
            )

        console.print(table)
        display.render_block_header("Approve Deletion?", "red")
        console.print(
            "[bold]Options:[/bold]\n"
            "  Enter number and decision:  [cyan]1:1[/cyan] (approve)  "
            "[cyan]1:2[/cyan] (skip)\n"
            "  Batch all:  [cyan]all:1[/cyan] (approve all)  "
            "[cyan]all:2[/cyan] (skip all)\n"
            "  When done:  [cyan]done[/cyan]\n\n"
            "[dim]Or edit the .response file in se3/calls/ from another terminal.[/dim]"
        )
        console.print("")
        display.render_block_footer("red")

    def _read_line_interruptible(self) -> Optional[str]:
        """Read a line from stdin, checking stop/done events periodically."""
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
        decisions: Dict[str, str] = {}
        total = len(self._pending_items)
        decision_map = {"1": "approve", "2": "skip"}

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
                    print(f"  {remaining} item(s) still unresolved. Defaulting to skip.")
                    for idx, item in enumerate(self._pending_items):
                        if item.item_id not in decisions:
                            decisions[item.item_id] = "skip"
                return decisions

            if line.lower().startswith("all:"):
                val = line.split(":", 1)[1].strip()
                if val not in decision_map:
                    print(f"  Invalid decision '{val}'. Use 1 (approve) or 2 (skip).")
                    continue
                decision = decision_map[val]
                for item in self._pending_items:
                    decisions[item.item_id] = decision
                print(f"  All {total} items set to {decision}.")
                return decisions

            if ":" in line:
                parts = line.split(":", 1)
                try:
                    num = int(parts[0].strip())
                except ValueError:
                    print("  Invalid format. Use '<number>:<1|2>' or 'all:<1|2>'.")
                    continue

                val = parts[1].strip()
                if val not in decision_map:
                    print(f"  Invalid decision '{val}'. Use 1 (approve) or 2 (skip).")
                    continue

                if num < 1 or num > total:
                    print(f"  Invalid item number {num}. Range: 1-{total}.")
                    continue

                item = self._pending_items[num - 1]
                decisions[item.item_id] = decision_map[val]
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
                        decisions = self._parse_response_file(
                            response_path, content=content
                        )
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
        """Generate a ``sync_high_impact_deletion`` MCP call file."""
        calls_dir = self.project_root / "se3" / "calls"
        calls_dir.mkdir(parents=True, exist_ok=True)

        timestamp = int(datetime.now().timestamp())
        unique_id = uuid.uuid4().hex[:8]
        call_file = calls_dir / f"sync_deletion_{timestamp}_{unique_id}.json"

        items = []
        for idx, item in enumerate(self._pending_items, 1):
            items.append({
                "id": idx,
                "item_id": item.item_id,
                "spec_name": item.spec_name,
                "requirement_name": item.requirement_name,
                "excerpt": item.requirement_excerpt,
                "options": ["approve", "skip"],
                "decision": "pending",
            })

        call_data = {
            "type": "sync_high_impact_deletion",
            "timestamp": timestamp,
            "items": items,
        }

        call_file.write_text(
            json.dumps(call_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        logger.info("Generated high-impact deletion call file: %s", call_file)
        return call_file

    def _parse_response_file(
        self, response_path: Path, *, content: Optional[str] = None,
    ) -> Optional[Dict[str, str]]:
        """Parse a ``.response`` file into a ``{item_id: 'approve'|'skip'}`` dict.

        Expected format::

            {
              "items": [
                {"id": 1, "item_id": "del_auth_abc12345", "decision": "approve"},
                ...
              ]
            }

        Returns ``None`` if parsing fails or the file holds no usable items.
        Items missing a decision default to ``"skip"`` (safe default — no
        deletion is applied).
        """
        try:
            raw = (
                content if content is not None
                else response_path.read_text(encoding="utf-8")
            )
            data = json.loads(raw)
        except (json.JSONDecodeError, OSError):
            return None

        resp_items = data.get("items", [])
        if not resp_items:
            return None

        decisions: Dict[str, str] = {}
        id_to_item_id: Dict[int, str] = {}
        expected_item_ids: set[str] = set()
        for idx, item in enumerate(self._pending_items, 1):
            id_to_item_id[idx] = item.item_id
            expected_item_ids.add(item.item_id)

        for resp in resp_items:
            decision = (resp.get("decision") or "").lower()
            if decision not in _VALID_DECISIONS:
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
                "Response file missing item_ids (defaulting to skip): %s",
                missing,
            )
            for item_id in missing:
                decisions[item_id] = "skip"

        return decisions

    def _write_response_file(self, decisions: Dict[str, str]) -> None:
        """Write decisions back to the ``.response`` file atomically."""
        if self._call_file_path is None:
            return

        response_path = Path(str(self._call_file_path) + ".response")

        items = []
        for idx, item in enumerate(self._pending_items, 1):
            items.append({
                "id": idx,
                "item_id": item.item_id,
                "decision": decisions.get(item.item_id, "skip"),
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
