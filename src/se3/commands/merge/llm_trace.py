"""Per-LLM-call trace logging for merge operations.

Records every LLM call issued during ``se3 merge`` as a single JSON
Lines (jsonl) record.  Each record includes the prompt, response,
duration, agent identifier, and outcome.  The trace file is
append-only, fsync'd after each record, and safe for concurrent
access within a single process (protected by a threading lock).

Files are named ``merge_<timestamp>_<seq>.jsonl`` under
``se3/logs/llm/`` so that a long-running merge sequence does not
produce unbounded single files.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Default directory for LLM trace files, relative to project root.
_DEFAULT_TRACE_DIR = Path("se3/logs/llm")

# Rotate to a new file when the current one exceeds this size.
_MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MiB


@dataclass
class LLMCallRecord:
    """A single LLM call record.

    Fields are chosen to be forward-compatible with future agent
    protocols while remaining useful for forensic analysis.
    """

    seq: int
    timestamp_iso: str
    agent: str
    prompt_tokens_est: Optional[int] = None
    # Prompt text (may be truncated for size).  Full prompts are
    # usually too large for inline jsonl; store a preview here.
    prompt_preview: str = ""
    # Response text (may be truncated for size).
    response_preview: str = ""
    # Duration in seconds (float).
    duration_sec: float = 0.0
    # Outcome: "success", "error", "timeout", "retry", "cancelled".
    outcome: str = ""
    # Optional error detail.
    error: Optional[str] = None
    # Arbitrary metadata (model name, temperature, etc.).
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp_iso,
            "agent": self.agent,
            "prompt_tokens_est": self.prompt_tokens_est,
            "prompt_preview": self.prompt_preview,
            "response_preview": self.response_preview,
            "duration_sec": self.duration_sec,
            "outcome": self.outcome,
            "error": self.error,
            "meta": self.meta,
        }


class LLMTrace:
    """Append-only jsonl trace writer for LLM calls during merge.

    Thread-safe within a single process.  Not safe across processes
    (the merge lock in ``merge_lock.py`` already prevents concurrent
    ``se3 merge`` invocations).

    Usage:

        trace = LLMTrace(project_root)
        trace.start()
        try:
            trace.record(prompt="...", response="...", duration_sec=1.2)
        finally:
            trace.stop()
    """

    def __init__(
        self,
        project_root: Path,
        trace_dir: Optional[Path] = None,
        max_file_bytes: int = _MAX_FILE_BYTES,
    ) -> None:
        self.project_root = project_root
        self.trace_dir = trace_dir or _DEFAULT_TRACE_DIR
        if not self.trace_dir.is_absolute():
            self.trace_dir = self.project_root / self.trace_dir
        self.max_file_bytes = max_file_bytes
        self._seq: int = 0
        # Per-file rotation counter, incremented on every new path.
        # Decoupling this from ``self._seq`` (which counts records, not
        # rotations) guarantees uniqueness even if two rotations land in
        # the same microsecond — without it the filename collides on
        # repeated rotations between record writes.
        self._rotation_seq: int = 0
        self._lock = threading.RLock()
        self._file: Optional = None  # type: ignore[type-arg]
        self._current_path: Optional[Path] = None
        self._started: bool = False

    def _ensure_dir(self) -> None:
        self.trace_dir.mkdir(parents=True, exist_ok=True)

    def _new_file_path(self) -> Path:
        # UTC for timestamp stability across DST shifts and machines in
        # different time zones; per-file rotation counter for uniqueness.
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        rotation = self._rotation_seq
        self._rotation_seq += 1
        return self.trace_dir / f"merge_{ts}_{rotation:06d}.jsonl"

    def _rotate_if_needed(self) -> None:
        if self._file is None:
            return
        try:
            pos = self._file.tell()
        except OSError:
            return
        if pos >= self.max_file_bytes:
            self._close_file()
            self._open_file()

    def _open_file(self) -> None:
        self._ensure_dir()
        self._current_path = self._new_file_path()
        self._file = open(self._current_path, "w", encoding="utf-8")
        logger.debug("Opened LLM trace: %s", self._current_path)

    def _close_file(self) -> None:
        if self._file is not None:
            try:
                self._file.flush()
                os.fsync(self._file.fileno())
            except OSError:
                pass
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None

    def start(self) -> None:
        """Open the trace file.  Idempotent."""
        with self._lock:
            if self._started:
                return
            self._open_file()
            self._started = True

    def stop(self) -> None:
        """Close the trace file and release resources."""
        with self._lock:
            self._close_file()
            self._started = False

    def record(
        self,
        *,
        agent: str = "",
        prompt: str = "",
        response: str = "",
        duration_sec: float = 0.0,
        outcome: str = "",
        error: Optional[str] = None,
        prompt_tokens_est: Optional[int] = None,
        meta: Optional[dict[str, Any]] = None,
        preview_chars: int = 2000,
    ) -> int:
        """Append one LLM call record to the trace.

        Returns the sequence number assigned to this record.
        """
        with self._lock:
            if not self._started:
                self.start()
            self._seq += 1
            seq = self._seq

            # Truncate previews to keep jsonl line sizes bounded.
            prompt_preview = prompt[:preview_chars] if prompt else ""
            response_preview = response[:preview_chars] if response else ""

            record = LLMCallRecord(
                seq=seq,
                timestamp_iso=datetime.now().isoformat(),
                agent=agent,
                prompt_tokens_est=prompt_tokens_est,
                prompt_preview=prompt_preview,
                response_preview=response_preview,
                duration_sec=duration_sec,
                outcome=outcome,
                error=error,
                meta=meta or {},
            )

            line = json.dumps(record.to_dict(), ensure_ascii=False, default=str) + "\n"

            if self._file is None:
                self._open_file()
            # Rotate BEFORE writing if the current file is already at
            # or over the limit.  This ensures each file stays under
            # the limit (the record that triggers rotation goes into
            # the new file, not the old one).
            self._rotate_if_needed()
            self._file.write(line)
            # Per-record fsync for durability.
            self._file.flush()
            os.fsync(self._file.fileno())
            return seq

    def __enter__(self) -> LLMTrace:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
