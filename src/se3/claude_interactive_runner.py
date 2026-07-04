"""Interactive Claude Code CLI adapter (PTY-driven runner).

Provides a third concrete :class:`AgentRunner` implementation alongside
``ClaudeCodeRunner`` (the ``-p`` print-mode adapter) and ``CodexRunner``.
Instead of driving ``claude -p`` in print mode, this runner launches the
**interactive** ``claude`` TUI inside a pseudo-terminal (PTY) and feeds the
effective prompt as simulated user input, mirroring how a human would type
into the input box.

Design split (mirrors ``codex_runner.py``):

* **PTY process driving / lifecycle control** — spawn the interactive
  ``claude`` in its own process session, hold the handle, feed the prompt on
  one background daemon thread, drain PTY output on another, and own the full
  lifecycle (wall / inactivity double timeout, force-kill of the whole process
  group, ``finally``-guaranteed reclamation, I/O thread join).  This is the
  scope of group G1 (this file's initial cut).
* **JSONL transcript watching / parsing** — locating and tailing the session
  transcript JSONL that interactive Claude writes to disk, and producing the
  Claude stream-json NDJSON the upstream consumers expect.  Group G2 wires this
  in: the session file is located by snapshotting the munged-cwd projects
  directory before launch and diffing afterwards (with a ``cwd`` second-pass
  check to isolate concurrent flows), records are tailed incrementally, each
  line's ``type`` is filtered and its ``.message`` stripped of the JSONL wrapper
  to yield stream-json NDJSON, ``usage`` tokens are accumulated and a terminal
  ``type:"result"`` line synthesized, and turn completion is detected from a
  composite of PTY silence + JSONL write-stop + a terminal last record.  The
  composite (PTY + JSONL) activity also feeds the inactivity-timeout signal.

The ``pexpect`` dependency is imported lazily (only when a PTY is actually
spawned) so that constructing the runner — or merely importing this module —
never requires ``pexpect`` to be installed.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from .agent_runner import AgentRunner, InfraErrorType
from .config import load_claude_commands, load_claude_subprocess_config

# Keywords indicating usage/rate limit in Claude output (shared with the
# print-mode runner's taxonomy).  Scanned by ``detect_infra_error`` against the
# combined PTY-output / JSONL-derived stream (NOT print-mode stdout).
USAGE_LIMIT_KEYWORDS = [
    "usage limit",
    "rate limit",
    "too many requests",
    "rate_limit",
    "overloaded",
    "capacity",
    "hit your limit",
    "you've hit your limit",
]

# Markers that indicate the interactive launch never got off the ground — either
# the PTY child failed to spawn or its session transcript was never written to
# disk.  ``detect_infra_error`` scans for these (case-insensitively) to classify
# a ``STARTUP_FAILURE``.  The signal source is the PTY output buffer + the JSONL
# location state, NOT print-mode stdout: ``_run_single_with_monitor`` appends the
# ``Failed to start`` line on a spawn error and the ``session transcript never
# created`` line when an active watcher never located its file.
STARTUP_FAILURE_KEYWORDS = (
    "failed to start",
    "session transcript never created",
)

# Main-loop poll interval (seconds) for the lifecycle supervisor.  Small enough
# that wall / inactivity timeouts fire promptly without busy-spinning.
_POLL_INTERVAL = 0.1

# Grace period (seconds) between SIGTERM and SIGKILL when terminating a PTY
# process group.
_TERM_GRACE = 0.2

# Conservative silence window (seconds) used to decide a single turn has
# finished: both the PTY output and the JSONL transcript must have been quiet
# for at least this long before a terminal last-record is trusted as "turn
# complete".  A generous default avoids mistaking Claude's brief mid-turn
# pauses (thinking / waiting on a tool) for the end of the turn.
TURN_SILENCE_WINDOW = 2.0

# Bracketed-paste control sequences (DEC 2004).  The effective prompt is large
# and multi-line; wrapping it in bracketed paste makes the interactive TUI
# insert the embedded newlines as literal input text rather than interpreting
# each one as an Enter keypress (which would dispatch the turn at the first
# line and mis-submit the rest as separate turns).
_BRACKET_PASTE_START = "\x1b[200~"
_BRACKET_PASTE_END = "\x1b[201~"

# Prompt delivery is written to the PTY master in bounded chunks with a tiny
# inter-chunk pause so a payload larger than the PTY input buffer is not
# silently truncated and the TUI's paste handler is not overrun.
_FEED_CHUNK_SIZE = 1024
_FEED_CHUNK_DELAY = 0.01

# Input-box readiness: the feed thread waits until the PTY has rendered output
# and then gone quiet for this settle window (the input box finished drawing),
# capped by an overall readiness timeout after which it proceeds best-effort.
# This replaces a fixed startup sleep that dropped the prompt on the floor when
# a cold / loaded machine took longer than the sleep to render the input box.
_INPUT_READY_SETTLE = 0.4
_INPUT_READY_TIMEOUT = 15.0

# The four token-count fields carried on Claude ``usage`` payloads, in the order
# the upstream usage accounting (``parse_usage_from_ndjson`` / ``UsageTotals``)
# reads them.
USAGE_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

# Assistant ``stop_reason`` values that mark the end of a turn (no further
# assistant output is coming).  ``"tool_use"`` is deliberately excluded — it
# means a tool call is pending and the turn continues.
_TERMINAL_STOP_REASONS = frozenset({"end_turn", "stop_sequence", "max_tokens"})


def _import_pexpect() -> Any:
    """Import ``pexpect`` lazily, with a friendly error if it is missing.

    Imported only when a PTY is actually spawned/drained, so constructing the
    runner (or importing this module) never requires ``pexpect`` to be present.
    """
    try:
        import pexpect  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised only when missing
        raise RuntimeError(
            "The 'claude-interactive' runner requires the 'pexpect' package. "
            "Install it with: pip install 'pexpect>=4.8.0'"
        ) from exc
    return pexpect


def _safe_isalive(handle: Any) -> bool:
    """Return whether the PTY child is still running, swallowing errors.

    ``pexpect.spawn.isalive()`` can raise once the child has been reaped; a
    dead/closed handle is treated as not alive.
    """
    if handle is None:
        return False
    try:
        return bool(handle.isalive())
    except Exception:
        return False


class _ActivityClock:
    """Thread-safe last-activity timestamp.

    The drain thread bumps it on every PTY read; the supervisor loop reads it
    to decide whether the inactivity timeout has elapsed.  In G1 the only
    activity source is PTY output; G2/G3 fold in JSONL write activity so the
    composite ``last_activity`` is the more-recent of the two.
    """

    def __init__(self) -> None:
        self._t = time.time()
        self._lock = threading.Lock()

    def update(self) -> None:
        with self._lock:
            self._t = time.time()

    def last(self) -> float:
        with self._lock:
            return self._t


# Cosmetic byte noise an interactive TUI emits even when it is making no real
# progress: ANSI CSI / OSC escape sequences and other two-char escapes.  These
# (plus carriage returns and the footer's volatile digit counters, handled in
# :func:`_normalize_pty_lines`) are what let a spinner / footer re-render keep a
# naive PTY activity clock pinned near ``now`` forever.  Stripping them collapses
# a re-render to a constant string so repeated re-renders read as "no new
# content".
_ANSI_SEQ_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"          # CSI ... final byte
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL / ST
    r"|\x1b[@-Z\\-_]"                      # other two-char escapes
)
_VOLATILE_DIGITS_RE = re.compile(r"\d+")


def _normalize_pty_lines(text: str) -> List[str]:
    """Strip cosmetic TUI noise and return the normalized *meaningful* lines.

    ANSI escape sequences are removed, carriage returns are treated as line
    breaks (a TUI overwrites the same screen row via ``\\r`` rather than ``\\n``),
    runs of digits are collapsed to ``#`` (so the footer's animated
    elapsed-seconds / token counters do not read as fresh content every second),
    inner whitespace is collapsed, and empty lines are dropped.  Two spinner /
    footer re-renders that differ only in their animation frame or counters
    normalize to the same small set of strings, while genuinely streamed
    assistant text yields continuously distinct strings.
    """
    cleaned = _ANSI_SEQ_RE.sub("", text).replace("\r", "\n")
    out: List[str] = []
    for raw in cleaned.split("\n"):
        norm = " ".join(_VOLATILE_DIGITS_RE.sub("#", raw).split())
        if norm:
            out.append(norm)
    return out


class _MeaningfulContentTracker:
    """Degraded-mode (no-transcript) progress detector over the raw PTY stream.

    Used only when no session-transcript JSONL is available (no ``cwd`` / the
    transcript has not been located yet).  In that mode the JSONL turn-end and
    write-activity signals do not exist, so the supervisor would otherwise key
    its inactivity-hang timer off the raw PTY :class:`_ActivityClock`.  That
    clock is useless here: the interactive TUI re-renders its spinner / footer
    roughly once a second *forever* — even on a finished or stalled turn — so a
    clock bumped on every PTY read can never go stale and, with
    ``wall_timeout=None`` (what ``LLMCaller`` passes) and an interactive
    ``claude`` that never self-exits after a turn, the supervisor loop would spin
    indefinitely.

    This tracker collapses cosmetic re-renders to a recently-seen set (see
    :func:`_normalize_pty_lines`) so only genuinely new printable content
    advances :attr:`clock`.  That gives the degraded path an inactivity signal
    the TUI re-render noise cannot keep alive, so a hung *or* a finished turn
    eventually trips the inactivity timeout and returns control.
    """

    # Bound large enough to hold a full spinner-frame cycle plus the footer
    # variants, so steady-state re-renders are all "seen" within ~1-2s.
    _SEEN_MAX = 256

    def __init__(self) -> None:
        self.clock = _ActivityClock()
        self._chunk_idx = 0
        self._residual = ""
        self._seen: "deque[str]" = deque(maxlen=self._SEEN_MAX)
        self._seen_set: Set[str] = set()

    def scan(self, output_buffer: List[str]) -> None:
        """Consume newly-appended PTY chunks and bump :attr:`clock` on new text.

        Safe to call repeatedly from the supervisor loop.  Reads only the
        already-appended tail of ``output_buffer`` (chunk-append by the drain
        thread is concurrency-safe to read by index here); a trailing partial
        line is held back until its terminator arrives so a single logical line
        split across polls is not double-counted.
        """
        n = len(output_buffer)
        if n <= self._chunk_idx:
            return
        new_text = self._residual + "".join(output_buffer[self._chunk_idx:n])
        self._chunk_idx = n

        last_sep = max(new_text.rfind("\r"), new_text.rfind("\n"))
        if last_sep == -1:
            # No complete line yet; carry everything forward.
            self._residual = new_text
            return
        complete = new_text[: last_sep + 1]
        self._residual = new_text[last_sep + 1:]

        fresh = False
        for line in _normalize_pty_lines(complete):
            if line not in self._seen_set:
                fresh = True
                if len(self._seen) == self._seen.maxlen:
                    self._seen_set.discard(self._seen[0])
                self._seen.append(line)
                self._seen_set.add(line)
        if fresh:
            self.clock.update()


# ---------------------------------------------------------------------------
# JSONL transcript watching & parsing (group G2)
#
# Interactive Claude Code writes a session transcript to
# ``~/.claude/projects/<munged-cwd>/<session>.jsonl``.  Each line is a wrapped
# record whose ``type`` ("assistant" / "user" / "system" / "summary" / ...) and
# (for assistant/user) ``.message`` payload form a *superset* of the stream-json
# NDJSON the print-mode runner gets from ``--output-format stream-json``.  The
# helpers below locate that file, tail it incrementally, strip the JSONL wrapper
# down to the stream-json lines the upstream consumers expect, accumulate token
# ``usage``, and decide when a single turn has completed.
# ---------------------------------------------------------------------------


def munge_cwd(cwd: Union[str, Path]) -> str:
    """Reproduce Claude Code's absolute-cwd → projects-subdir-name munging.

    Claude Code derives the per-project transcript directory name from the
    absolute working directory by replacing every non-alphanumeric character
    with ``-``.  For example ``/data/cre/workspace/se3.0`` becomes
    ``-data-cre-workspace-se3-0`` (both the path separators and the ``.`` map to
    ``-``).  This is a best-effort reproduction; :func:`locate_session_file`
    falls back to a whole-``projects``-tree search when the rule no longer holds.
    """
    text = str(cwd)
    return re.sub(r"[^A-Za-z0-9]", "-", text)


def claude_projects_dir() -> Path:
    """Return the ``~/.claude/projects`` directory (honoring ``CLAUDE_CONFIG_DIR``).

    Claude Code stores session transcripts under ``<config>/projects``, where
    ``<config>`` defaults to ``~/.claude`` but can be overridden via the
    ``CLAUDE_CONFIG_DIR`` environment variable.
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    if base:
        return Path(base).expanduser() / "projects"
    return Path.home() / ".claude" / "projects"


def _safe_mtime(path: Path) -> float:
    """Return ``path``'s mtime, or ``0.0`` if it cannot be stat-ed."""
    try:
        return path.stat().st_mtime
    except Exception:
        return 0.0


def snapshot_session_files(projects_dir: Path) -> Set[Path]:
    """Snapshot the set of ``*.jsonl`` transcript files under ``projects_dir``.

    Recurses the whole ``projects`` tree (not just one munged subdir) so the
    pre/post-launch diff in :func:`locate_session_file` still works when the
    munged-dir rule has drifted.  Best-effort — a missing directory or a stat
    error yields an empty / partial set rather than raising.
    """
    out: Set[Path] = set()
    try:
        if not projects_dir.exists():
            return out
        for p in projects_dir.rglob("*.jsonl"):
            out.add(p)
    except Exception:
        pass
    return out


def _file_cwd_matches(
    path: Path,
    cwd: Union[str, Path],
    max_lines: int = 50,
) -> Optional[bool]:
    """Second-pass ``cwd`` validation of a candidate transcript file.

    Reads up to ``max_lines`` records and inspects their ``cwd`` field.

    Returns:
        ``True``  — a record's ``cwd`` matches ``cwd`` (this is our file);
        ``False`` — a ``cwd`` field was present but never matched (a *different*
                    flow's file — used to exclude concurrent-flow transcripts);
        ``None``  — no ``cwd`` field was found yet (ambiguous: a freshly created
                    file may not have written one), or the file is unreadable.
    """
    try:
        target = str(Path(cwd))
        target_resolved: Optional[str] = None
        try:
            target_resolved = str(Path(cwd).resolve())
        except Exception:
            target_resolved = None

        found_any_cwd = False
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                c = rec.get("cwd")
                if isinstance(c, str) and c:
                    found_any_cwd = True
                    if c == target or (
                        target_resolved is not None and c == target_resolved
                    ):
                        return True
        return False if found_any_cwd else None
    except Exception:
        return None


def locate_session_file(
    projects_dir: Path,
    pre_snapshot: Set[Path],
    cwd: Union[str, Path],
    session_id: Optional[str] = None,
) -> Optional[Path]:
    """Locate this launch's ``<session>.jsonl`` transcript.

    When ``session_id`` is provided (the normal path — the runner launches
    interactive ``claude`` with an explicit ``--session-id <uuid>``), the file
    is bound **deterministically** to ``<munged-cwd>/<session_id>.jsonl``.  This
    is the only race-free way to isolate concurrent flows that share the same
    ``cwd``: a ``cwd``-field match cannot disambiguate two transcripts written
    by two flows in the same working directory, but the per-launch UUID is
    unique to one flow, so each binds to its own file and never to a sibling's.
    Returns ``None`` until that exact file appears.

    When ``session_id`` is ``None`` (degraded fallback — no session id was
    assigned), the file is found by diffing the current ``*.jsonl`` set against
    ``pre_snapshot`` (captured *before* launch).  New files inside the
    munged-cwd subdirectory are preferred; the ``cwd`` field is used as a
    second-pass check to drop transcripts that explicitly name a different
    directory.  When the munged-cwd directory has no new file, the search falls
    back to the whole ``projects`` tree but then *requires* a positive ``cwd``
    match.  Returns ``None`` when nothing matches.
    """
    current = snapshot_session_files(projects_dir)

    if session_id:
        # Deterministic, race-free isolation: bind only to the exact transcript
        # named after this launch's unique session id.
        target_dir = projects_dir / munge_cwd(cwd)
        expected = target_dir / f"{session_id}.jsonl"
        if expected in current:
            return expected
        # Tolerate munged-dir drift (a claude version that writes under a
        # slightly different munged path): match the unique stem anywhere in the
        # tree.  The stem is the UUID, so this stays isolated to our own flow.
        for p in current:
            if p.stem == session_id:
                return p
        return None

    new_files = [p for p in current if p not in pre_snapshot]
    if not new_files:
        return None

    target_dir = projects_dir / munge_cwd(cwd)
    primary = [p for p in new_files if p.parent == target_dir]

    if primary:
        # Inside the expected munged dir: trust it, but drop any new file whose
        # cwd explicitly names a *different* directory (concurrent flow).
        kept = [p for p in primary if _file_cwd_matches(p, cwd) is not False]
        pool = kept or primary
        return max(pool, key=_safe_mtime)

    # Munged-dir rule produced nothing — fall back to the whole tree, but only
    # accept files whose cwd positively matches (strict isolation).
    matched = [p for p in new_files if _file_cwd_matches(p, cwd) is True]
    if matched:
        return max(matched, key=_safe_mtime)
    return None


def tail_new_records(path: Path, cursor: int) -> Tuple[List[dict], int]:
    """Incrementally read JSON records appended to ``path`` since byte ``cursor``.

    Reads from the byte offset ``cursor`` to end-of-file, parsing only *complete*
    lines (a trailing partial line with no newline is left unconsumed, so the
    cursor never advances past half-written records).  Malformed / non-dict
    lines are skipped.  A file that shrank below ``cursor`` (rotation/truncation)
    resets the cursor to ``0``.

    Returns ``(records, new_cursor)``.
    """
    records: List[dict] = []
    try:
        size = path.stat().st_size
    except Exception:
        return records, cursor

    if size < cursor:
        cursor = 0
    if size == cursor:
        return records, cursor

    try:
        with open(path, "rb") as fh:
            fh.seek(cursor)
            data = fh.read()
    except Exception:
        return records, cursor

    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        # No complete line yet — leave the cursor where it was.
        return records, cursor

    complete = data[: last_nl + 1]
    new_cursor = cursor + last_nl + 1
    text = complete.decode("utf-8", errors="replace")
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records, new_cursor


def to_stream_json_ndjson(record: dict) -> Optional[str]:
    """Convert one JSONL transcript record to a stream-json NDJSON line.

    Filters by ``type`` and strips the JSONL wrapper down to the ``{"type",
    "message"}`` shape the upstream consumers (``StreamJSONTracker``,
    ``extract_assistant_text``, ``parse_usage_from_ndjson``, ``_last_touched_files``)
    already understand:

    * ``assistant`` → ``{"type": "assistant", "message": <api message>}`` (the
      full message object: ``model`` / ``role`` / ``content`` with
      ``text`` / ``thinking`` / ``tool_use`` blocks / ``stop_reason`` / ``usage``).
    * ``user`` → ``{"type": "user", "message": <message>}`` **only** when the
      message carries ``tool_result`` blocks (so tool results flow through);
      a plain-text user record (the echoed human prompt) is dropped.
    * ``result`` / ``init`` → passed through (already stream-json terminal /
      header lines, rare in a transcript but tolerated).
    * Everything else (``summary`` / ``system`` / ``file-history-snapshot`` /
      sidechain records / unknown) → dropped (``None``).

    Returns the serialized NDJSON line (no trailing newline) or ``None`` to drop.
    """
    if not isinstance(record, dict):
        return None
    # Sub-agent (Task tool) sidechain conversations live in the same file; keep
    # the main thread clean by dropping them.
    if record.get("isSidechain"):
        return None

    rtype = record.get("type")

    if rtype == "assistant":
        msg = record.get("message")
        if not isinstance(msg, dict):
            return None
        return json.dumps({"type": "assistant", "message": msg})

    if rtype == "user":
        msg = record.get("message")
        if not isinstance(msg, dict):
            return None
        content = msg.get("content")
        if isinstance(content, list) and any(
            isinstance(c, dict) and c.get("type") == "tool_result" for c in content
        ):
            return json.dumps({"type": "user", "message": msg})
        return None

    if rtype in ("result", "init"):
        try:
            return json.dumps(record)
        except Exception:
            return None

    return None


def extract_usage_from_record(record: dict) -> Dict[str, int]:
    """Read the four token counts from a record's ``usage`` payload.

    Prefers the nested ``message.usage`` shape (assistant transcript records)
    and falls back to a flat top-level ``usage``.  Missing fields default to
    ``0``; any parsing surprise is swallowed and yields all-zero counts, so
    usage extraction never disturbs the main path.
    """
    zeros = {k: 0 for k in USAGE_TOKEN_KEYS}
    try:
        usage = None
        msg = record.get("message") if isinstance(record, dict) else None
        if isinstance(msg, dict):
            usage = msg.get("usage")
        if not isinstance(usage, dict):
            top = record.get("usage") if isinstance(record, dict) else None
            if isinstance(top, dict):
                usage = top
        if not isinstance(usage, dict):
            return zeros
        out: Dict[str, int] = {}
        for k in USAGE_TOKEN_KEYS:
            try:
                out[k] = int(usage.get(k, 0) or 0)
            except Exception:
                out[k] = 0
        return out
    except Exception:
        return zeros


def synthesize_result_line(
    usage: Dict[str, int],
    total_cost_usd: float = 0.0,
) -> str:
    """Build a terminal ``type:"result"`` stream-json line from accumulated usage.

    The interactive transcript carries usage per assistant message but no
    synthesized ``result`` line (that is a print-mode artifact).  We mint one so
    the tokens flow through the existing ``StreamJSONTracker._capture_usage`` →
    ``add_call_usage`` → ``parse_usage_from_ndjson`` accounting chain unchanged.
    The cost is not reported by the transcript, so it defaults to ``0``.
    """
    payload: Dict[str, Any] = {
        "type": "result",
        "usage": {k: int(usage.get(k, 0) or 0) for k in USAGE_TOKEN_KEYS},
        "total_cost_usd": float(total_cost_usd or 0.0),
    }
    return json.dumps(payload)


def _is_terminal_record(record: Optional[dict]) -> bool:
    """Whether ``record`` marks the end of a turn.

    A ``type:"result"`` record, or an ``assistant`` record whose ``stop_reason``
    is a terminal value (``end_turn`` / ``stop_sequence`` / ``max_tokens``), ends
    the turn.  An assistant record with ``stop_reason == "tool_use"`` (tool call
    pending) or a ``user``/``tool_result`` record does NOT.
    """
    if not isinstance(record, dict):
        return False
    if record.get("type") == "result":
        return True
    if record.get("type") == "assistant":
        msg = record.get("message")
        if isinstance(msg, dict):
            return msg.get("stop_reason") in _TERMINAL_STOP_REASONS
    return False


def turn_complete(
    jsonl_idle: bool,
    last_record: Optional[dict],
) -> bool:
    """Single-turn completion check, driven by the JSONL transcript.

    A turn is complete when **both** of:

    * ``last_record`` is a terminal record (:func:`_is_terminal_record` —
      an assistant ``end_turn`` / ``stop_sequence`` / ``max_tokens``, or a
      synthesized ``result`` line); and
    * ``jsonl_idle`` — the transcript JSONL has stopped growing for the
      conservative :data:`TURN_SILENCE_WINDOW` (no records are still being
      written).

    The transcript is the authoritative signal for "the model finished": the
    interactive ``claude`` writes its terminal assistant record and then stops
    appending.  PTY idleness is deliberately **not** required — the TUI keeps
    re-rendering its footer / status bar / spinner indefinitely after the turn
    ends, so a PTY-silence gate would never clear and the supervisor loop would
    spin forever on a logically-finished turn (since the same cosmetic PTY
    output also keeps the inactivity timer fresh).

    The two false positives the composite must avoid are both covered without
    PTY input: the model still writing JSONL keeps ``jsonl_idle`` False, and a
    momentary lull between tool steps leaves ``last_record`` non-terminal
    (``stop_reason == "tool_use"`` or a ``tool_result`` user record).
    """
    return bool(jsonl_idle and _is_terminal_record(last_record))


class SessionTranscriptWatcher:
    """Stateful watcher over one session transcript JSONL.

    Composes the pure helpers above: snapshots the projects dir before launch,
    locates the new transcript after launch, tails it incrementally producing
    stream-json NDJSON, accumulates token ``usage``, synthesizes a terminal
    ``result`` line, and exposes JSONL write activity for the composite
    inactivity / turn-end signals.
    """

    def __init__(
        self,
        cwd: Union[str, Path],
        projects_dir: Optional[Path] = None,
        session_id: Optional[str] = None,
    ) -> None:
        self.cwd = cwd
        self.projects_dir = (
            Path(projects_dir) if projects_dir is not None else claude_projects_dir()
        )
        self.session_id = session_id
        self.path: Optional[Path] = None
        self._cursor = 0
        self._pre_snapshot: Set[Path] = set()
        self.line_count = 0
        self.last_record: Optional[dict] = None
        self.last_meaningful_record: Optional[dict] = None
        self._init_emitted = False
        self._usage: Dict[str, int] = {k: 0 for k in USAGE_TOKEN_KEYS}
        self._last_poll_activity = time.time()

    def snapshot(self) -> None:
        """Capture the pre-launch ``*.jsonl`` set (call before spawning)."""
        self._pre_snapshot = snapshot_session_files(self.projects_dir)

    def locate(self) -> bool:
        """Try to locate this launch's transcript file; idempotent once found."""
        if self.path is not None:
            return True
        p = locate_session_file(
            self.projects_dir, self._pre_snapshot, self.cwd, self.session_id
        )
        if p is not None:
            self.path = p
            return True
        return False

    def poll(self) -> List[str]:
        """Read new records and return the stream-json NDJSON lines they map to.

        Side effects: advances the byte cursor, bumps ``line_count``, updates
        ``last_record`` / ``last_meaningful_record``, accumulates token ``usage``
        from assistant messages, and synthesizes a leading ``init`` line
        (carrying the model) the first time a model name is seen so model-name
        extraction keeps working upstream.

        ``last_meaningful_record`` is advanced only for records that survive
        :func:`to_stream_json_ndjson` (assistant / result / user-with-tool_result),
        so turn-completion terminality is evaluated against the last *meaningful*
        record rather than a benign trailing ``system`` / ``summary`` /
        ``file-history-snapshot`` line written after the assistant's ``end_turn``.
        """
        lines: List[str] = []
        if self.path is None:
            return lines

        records, self._cursor = tail_new_records(self.path, self._cursor)
        if records:
            self._last_poll_activity = time.time()

        for rec in records:
            self.line_count += 1
            self.last_record = rec

            if isinstance(rec, dict) and rec.get("type") == "assistant":
                u = extract_usage_from_record(rec)
                for k in USAGE_TOKEN_KEYS:
                    self._usage[k] += u.get(k, 0)
                if not self._init_emitted:
                    msg = rec.get("message")
                    model = msg.get("model") if isinstance(msg, dict) else None
                    if isinstance(model, str) and model:
                        lines.append(json.dumps({"type": "init", "model": model}))
                        self._init_emitted = True

            nd = to_stream_json_ndjson(rec)
            if nd is not None:
                self.last_meaningful_record = rec
                lines.append(nd)

        return lines

    @property
    def usage(self) -> Dict[str, int]:
        """Accumulated token counts across all assistant messages seen so far."""
        return dict(self._usage)

    def write_activity(self) -> float:
        """Best timestamp of the last JSONL write activity (mtime-based).

        Uses the transcript file mtime when available (catching partial writes
        the line-cursor has not yet consumed), falling back to the last poll that
        yielded records.  Drives both the composite inactivity signal and the
        ``jsonl_idle`` half of :func:`turn_complete`.
        """
        if self.path is not None:
            m = _safe_mtime(self.path)
            if m > 0:
                return m
        return self._last_poll_activity

    def synthesize_result(self, total_cost_usd: float = 0.0) -> str:
        """Mint the terminal ``result`` line from accumulated usage."""
        return synthesize_result_line(self._usage, total_cost_usd)


class ClaudeInteractiveRunner(AgentRunner):
    """Interactive Claude Code CLI adapter — drives the TUI via a PTY.

    Wraps a single Claude CLI command (e.g. ``claude``) per instance; agent
    selection/rotation is owned by :class:`LLMCaller`.  The effective prompt is
    *not* embedded in argv (no ``-p``); it is stashed on the instance by
    :meth:`build_call_args` and fed into the PTY as simulated user input by
    :meth:`run_with_monitor`.
    """

    def __init__(
        self,
        project_root: Optional[Path] = None,
        commands: Optional[List[Dict[str, Any]]] = None,
        command: Optional[Dict[str, Any]] = None,
        setting_sources: Optional[List[str]] = None,
    ):
        """Initialize with a single command (mirrors ``ClaudeCodeRunner``).

        Args:
            project_root: Project root for loading config / setting sources.
            commands: Legacy list-of-dicts parameter; first entry is used when
                ``command`` is not given.
            command: Single command dict ``{cmd, priority}`` (preferred).
            setting_sources: Optional explicit Claude CLI setting-source list
                (subset of ``{"user", "project", "local"}``).  When ``None``,
                loaded from ``project_root``'s ``claude_subprocess`` config,
                falling back to ``["user"]``.  Injected into the launch argv via
                ``--setting-sources`` so SE3 workers are not constrained by a
                downstream project's ``.claude/settings.json``.
        """
        if command is not None:
            self.command = command
        elif commands is not None:
            self.command = commands[0] if commands else {"cmd": "claude", "priority": 0}
        else:
            all_commands = load_claude_commands(project_root)
            self.command = all_commands[0] if all_commands else {"cmd": "claude", "priority": 0}

        # Backward-compatible commands list view.
        if commands is not None:
            self.commands = commands
        else:
            self.commands = [self.command]

        if setting_sources is not None:
            self.setting_sources = list(setting_sources)
        elif project_root is not None:
            self.setting_sources = list(
                load_claude_subprocess_config(project_root).setting_sources
            )
        else:
            self.setting_sources = ["user"]
        self._setting_sources_arg = ",".join(self.setting_sources)

        self.project_root = project_root

        # The effective prompt to feed as user input, stashed by
        # build_call_args (interactive mode carries no -p in argv).
        self._pending_prompt: Optional[str] = None

        # Per-launch session id (a UUID).  Assigned at the start of each run /
        # run_with_monitor, injected into the launch argv as ``--session-id`` and
        # used by the transcript watcher to bind deterministically to this
        # launch's own ``<session_id>.jsonl`` — the race-free way to isolate
        # concurrent flows sharing the same cwd.
        self._session_id: Optional[str] = None

    # ------------------------------------------------------------------
    # build_call_args — intent → interactive-mode CLI flags
    # ------------------------------------------------------------------

    def build_call_args(
        self,
        prompt: str,
        read_only: bool,
        context_files: Optional[List[Path]] = None,
        spec_guard_plugin: Optional[Path] = None,
    ) -> List[str]:
        """Translate call intent into interactive-mode launch CLI flags.

        ``spec_guard_plugin`` is accepted for interface parity but currently
        ignored by this runner (the PreToolUse spec-write hook is injected via
        ``--plugin-dir`` by the print-mode ``ClaudeCodeRunner``).

        Unlike the print-mode runner, the prompt is NOT placed in argv (no
        ``-p`` / ``--output-format`` / ``--input-format`` — those are
        print-only).  The prompt is stashed on the instance and fed into the
        PTY as simulated user input by :meth:`run_with_monitor`.

        Flag translation:

        * Read-only steps append ``--disallowedTools Write Edit NotebookEdit
          AskUserQuestion`` (tool-layer read-only enforcement; available in
          interactive mode).  Writable steps add nothing here — write
          permission is granted by the ``--dangerously-skip-permissions``
          top-level flag injected into the launch argv prefix.
        * ``context_files`` are translated to ``--add-dir <parent>`` for each
          existing file's parent directory (deduplicated, order-preserving),
          the interactive-mode equivalent of print mode's ``--file`` — it makes
          the files reachable within the session.

        Args:
            prompt: The effective prompt text (fed as user input later).
            read_only: Whether the current step is read-only.
            context_files: Optional list of files to expose to the session.

        Returns:
            The interactive-mode CLI flag list (excluding the command name and
            the runner's own ``--dangerously-skip-permissions`` /
            ``--setting-sources`` prefix, which the execution methods prepend).
        """
        # Stash the prompt; it is fed via the PTY, not via argv.
        self._pending_prompt = prompt

        args: List[str] = []

        if read_only:
            args += [
                "--disallowedTools",
                "Write",
                "Edit",
                "NotebookEdit",
                "AskUserQuestion",
            ]

        if context_files:
            seen: set = set()
            for f in context_files:
                try:
                    if not f.exists():
                        continue
                    parent = str(f.parent)
                except Exception:
                    continue
                if parent in seen:
                    continue
                seen.add(parent)
                args.extend(["--add-dir", parent])

        return args

    def _build_full_cmd(self, args: List[str]) -> List[str]:
        """Compose the launch argv, injecting the runner's prefix flags.

        Mirrors ``ClaudeCodeRunner``'s injection convention: the interactive
        ``claude`` is always launched with ``--dangerously-skip-permissions``
        (so no permission popups block the PTY) and ``--setting-sources <csv>``
        (so a downstream project's settings cannot lock SE3 worker tools).
        """
        cmd_name = self.command["cmd"]
        full = [
            cmd_name,
            "--dangerously-skip-permissions",
            "--setting-sources",
            self._setting_sources_arg,
        ]
        # Pin an explicit session id so the transcript filename is known up
        # front; this is what lets the watcher bind to exactly this launch's
        # transcript and never a concurrent same-cwd flow's.
        if self._session_id:
            full += ["--session-id", self._session_id]
        return full + list(args)

    # ------------------------------------------------------------------
    # PTY process driving layer
    # ------------------------------------------------------------------

    def _spawn_pty(
        self,
        full_cmd: List[str],
        cwd: Optional[Path],
        env: Optional[Dict[str, str]],
    ) -> Any:
        """Spawn the interactive ``claude`` inside a PTY in its own session.

        ``pexpect.spawn`` (via ``ptyprocess``) calls ``os.setsid()`` in the
        child, so the child is a session/process-group leader.  That lets
        :meth:`_terminate` kill the *entire* process group on cleanup, reaping
        any tool/bash grandchildren rather than leaving orphans.

        Returns the ``pexpect.spawn`` handle.
        """
        pexpect = _import_pexpect()
        handle = pexpect.spawn(
            full_cmd[0],
            args=list(full_cmd[1:]),
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            encoding="utf-8",
            codec_errors="replace",
            echo=False,
            timeout=None,
            dimensions=(40, 120),
        )
        return handle

    def _feed_prompt(
        self,
        handle: Any,
        prompt: str,
        output_buffer: Optional[List[str]] = None,
        pty_clock: Optional["_ActivityClock"] = None,
        ready_timeout: float = _INPUT_READY_TIMEOUT,
        ready_delay: Optional[float] = None,
    ) -> None:
        """Feed ``prompt`` into the PTY as simulated user input.

        Runs on a background daemon thread.  It first waits for the interactive
        input box to become ready — preferring an observation of the PTY render
        timing (``output_buffer`` / ``pty_clock``, both fed by the drain
        thread) over a fixed sleep, so a slow/cold start does not type the
        prompt before the box exists — then delivers the *entire* multi-line
        prompt wrapped in a bracketed paste (so embedded newlines are inserted
        as input rather than treated as Enter), and finally sends a single
        carriage return to dispatch the turn.

        The payload is written in a partial-write-safe loop directly to the PTY
        master so a prompt larger than the PTY input buffer is fully delivered
        rather than silently truncated by ``pexpect.send``'s single
        ``os.write``.

        ``ready_delay`` is retained for backward compatibility / tests: when no
        readiness signals are supplied it falls back to a fixed sleep of that
        many seconds (``None`` / ``0`` means feed immediately).

        All failures are swallowed — a feed that cannot be delivered surfaces
        downstream as no output / a timeout rather than crashing the thread.
        """
        try:
            if output_buffer is not None or pty_clock is not None:
                self._await_input_ready(
                    output_buffer, pty_clock, ready_timeout, _INPUT_READY_SETTLE
                )
            elif ready_delay and ready_delay > 0:
                time.sleep(ready_delay)
            # Deliver the full prompt as one bracketed-paste user message...
            self._write_text(handle, _BRACKET_PASTE_START + prompt + _BRACKET_PASTE_END)
            # ...let the TUI register the paste, then submit with Enter.
            time.sleep(0.2)
            self._write_text(handle, "\r")
        except Exception:
            pass

    @staticmethod
    def _await_input_ready(
        output_buffer: Optional[List[str]],
        pty_clock: Optional["_ActivityClock"],
        ready_timeout: float,
        settle: float,
    ) -> None:
        """Block until the interactive input box looks ready, or time out.

        Readiness is inferred from PTY render timing rather than a fixed sleep:
        wait until the PTY has produced *some* output (the TUI started drawing)
        and then stayed quiet for ``settle`` seconds (the input box finished
        rendering and is idle).  If the readiness window elapses without that
        signal, return anyway so the feed proceeds best-effort.
        """
        deadline = time.time() + max(0.0, ready_timeout)
        saw_output = False
        while time.time() < deadline:
            if output_buffer:
                saw_output = True
            if saw_output:
                if pty_clock is not None:
                    if (time.time() - pty_clock.last()) >= settle:
                        return
                else:
                    time.sleep(settle)
                    return
            time.sleep(0.05)

    def _write_text(self, handle: Any, text: str) -> None:
        """Write ``text`` to the PTY master, guaranteeing full delivery.

        Routes through a byte-level partial-write loop on the child fd when one
        is available (real ``pexpect.spawn``); otherwise falls back to a single
        ``handle.send`` (test stubs that expose only ``send``).
        """
        fd = getattr(handle, "child_fd", None)
        if fd is None:
            handle.send(text)
            return
        self._write_all_bytes(fd, text.encode("utf-8"))

    @staticmethod
    def _write_all_bytes(fd: int, data: bytes) -> None:
        """Write every byte of ``data`` to ``fd``, retrying the remainder.

        ``os.write`` may write fewer bytes than requested (PTY buffer full),
        and ``pexpect.send`` does not retry the unwritten tail; this loop does,
        chunked with a tiny pause so a large payload neither truncates nor
        overruns the TUI's paste handler.
        """
        view = memoryview(data)
        sent_since_pause = 0
        while view:
            try:
                n = os.write(fd, view[:_FEED_CHUNK_SIZE])
            except (BlockingIOError, InterruptedError):
                time.sleep(_FEED_CHUNK_DELAY)
                continue
            if n <= 0:
                time.sleep(_FEED_CHUNK_DELAY)
                continue
            view = view[n:]
            sent_since_pause += n
            if view and sent_since_pause >= _FEED_CHUNK_SIZE:
                time.sleep(_FEED_CHUNK_DELAY)
                sent_since_pause = 0

    def _drain_pty(
        self,
        handle: Any,
        activity: _ActivityClock,
        stop_event: threading.Event,
        output_buffer: List[str],
        on_output: Optional[Callable[[str], None]],
        on_activity: Optional[Callable[[], None]],
        log_fh: Optional[Any] = None,
        pty_clock: Optional[_ActivityClock] = None,
    ) -> None:
        """Continuously read PTY output until EOF / stop, updating activity.

        Runs on a background daemon thread.  Every non-empty read bumps the
        composite :class:`_ActivityClock` (and, when supplied, the separate
        ``pty_clock`` used by the turn-end ``pty_idle`` signal), appends to
        ``output_buffer``, mirrors to the log file, and fires the
        ``on_output`` / ``on_activity`` callbacks.  Exits on EOF, when
        ``stop_event`` is set, or when the child dies.
        """
        pexpect = _import_pexpect()
        while not stop_event.is_set():
            try:
                data = handle.read_nonblocking(size=4096, timeout=1)
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF:
                break
            except Exception:
                # Handle closed / process gone — stop if no longer alive.
                if not _safe_isalive(handle):
                    break
                continue

            if not data:
                if not _safe_isalive(handle):
                    break
                continue

            activity.update()
            if pty_clock is not None:
                pty_clock.update()
            output_buffer.append(data)
            if log_fh is not None:
                try:
                    log_fh.write(data)
                    log_fh.flush()
                except Exception:
                    pass
            if on_output is not None:
                try:
                    on_output(data)
                except Exception:
                    pass
            if on_activity is not None:
                try:
                    on_activity()
                except Exception:
                    pass

    def _terminate(self, handle: Any) -> None:
        """Force-terminate the PTY child's whole process group and reap it.

        Equivalent to the print-mode runner's ``proc.kill() + proc.wait()`` but
        scoped to the *process group* so tool/bash grandchildren are not
        orphaned: SIGTERM the group, grace period, SIGKILL the group if still
        alive, then ``close(force=True)`` to run pexpect's own cleanup and reap
        the child.  Every step is best-effort; cleanup never raises.
        """
        if handle is None:
            return

        pid = getattr(handle, "pid", None)

        def _killpg(sig: int) -> None:
            if pid is None:
                return
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, sig)
            except (ProcessLookupError, OSError):
                pass

        # 1. Polite group terminate.
        if _safe_isalive(handle):
            _killpg(signal.SIGTERM)

        # 2. Grace period, then hard group kill if still alive.
        if _safe_isalive(handle):
            try:
                time.sleep(_TERM_GRACE)
            except Exception:
                pass
        if _safe_isalive(handle):
            _killpg(signal.SIGKILL)

        # 3. pexpect's own close (sends signals + waits/reaps the child).
        try:
            handle.close(force=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # run — synchronous execution (delegates to the monitored path)
    # ------------------------------------------------------------------

    def run(
        self,
        args: List[str],
        timeout: Optional[int] = None,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        on_retry: Optional[Callable[[int, str], Optional[List[str]]]] = None,
    ) -> subprocess.CompletedProcess:
        """Run one interactive turn synchronously and return a ``CompletedProcess``.

        Reuses the same PTY driving + JSONL parsing path as
        :meth:`run_with_monitor` by invoking :meth:`_run_single_with_monitor`
        with ``wall_timeout=timeout`` for a single bounded turn.  The returned
        ``stdout`` is the stream-json NDJSON produced from the transcript (no
        ``=== Command: ===`` prefix — that wrapper is the monitored path's
        concern), so callers that prefer the ``CompletedProcess`` shape consume
        the same byte stream the monitored path emits.

        * ``timeout`` maps to the wall timeout — a breach returns a synthetic
          ``CompletedProcess`` with ``returncode=124`` (matching ``timeout(1)``).
        * The child environment is copied and ``CLAUDECODE`` is scrubbed so the
          parent's own Claude Code session does not leak into the child.
        * ``on_retry`` is ignored (kept only for interface compatibility; agent
          rotation lives in :class:`LLMCaller`).
        """
        cmd_name = self.command["cmd"]

        run_env = env if env is not None else dict(os.environ)
        run_env.pop("CLAUDECODE", None)

        prompt = self._pending_prompt or ""
        self._session_id = str(uuid.uuid4())
        full_cmd = self._build_full_cmd(args)

        result = self._run_single_with_monitor(
            full_cmd=full_cmd,
            cmd_name=cmd_name,
            prompt=prompt,
            log_file=None,
            wall_timeout=timeout,
            inactivity_timeout=1800,
            cwd=cwd,
            env=run_env,
            on_output=None,
            on_activity=None,
            start_time=time.time(),
        )
        return subprocess.CompletedProcess(
            args=full_cmd,
            returncode=result.returncode,
            stdout=result.output,
            stderr="",
        )

    # ------------------------------------------------------------------
    # run_with_monitor — PTY lifecycle control + activity supervision
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
        """Launch interactive ``claude`` in a PTY and supervise its lifecycle.

        Owns the full process lifecycle, matching the print-mode runner's
        guarantees:

        * **Controlled start / held handle** — spawns the PTY and keeps the
          handle, queryable for liveness at any time.
        * **Wall timeout** — kills the process group and returns
          ``returncode=124`` once total runtime exceeds ``wall_timeout``.
        * **Inactivity timeout** — kills and returns ``returncode=124`` once no
          activity is seen for ``inactivity_timeout`` seconds (default 1800).
          In G1 the activity signal is PTY output only; G2/G3 fold in JSONL
          write activity.
        * **Force termination + reclamation** — a ``finally`` block always runs
          :meth:`_terminate` (SIGTERM/SIGKILL of the whole process group +
          reap) and joins the feed/drain I/O threads, so PTY, child, and
          grandchildren are cleaned up on every exit path with no orphans.

        Args:
            args: Interactive-mode CLI flags from :meth:`build_call_args`.
            log_file: Optional path to mirror PTY output to.
            wall_timeout: Max total runtime in seconds (``None`` = unbounded).
            inactivity_timeout: Seconds of silence before declaring a hang.
            cwd: Working directory for the session.
            env: Environment variables for the child.
            on_output: Callback for each chunk of PTY output.
            on_activity: Callback fired on each activity tick.
            on_confirm: Accepted for interface compatibility; interactive mode
                with ``--dangerously-skip-permissions`` does not raise
                permission popups, so this is currently a no-op.

        Returns:
            A :class:`MonitoredResult` carrying the exit code and the captured
            PTY output (prefixed ``=== Command: <cmd> ===``).
        """
        start_time = time.time()
        cmd_name = self.command["cmd"]

        run_env = env
        if run_env is None:
            run_env = dict(os.environ)
        run_env.pop("CLAUDECODE", None)

        prompt = self._pending_prompt or ""

        self._session_id = str(uuid.uuid4())
        full_cmd = self._build_full_cmd(args)

        print(f"[claude-interactive] Running command: '{cmd_name}'", file=sys.stderr)

        result = self._run_single_with_monitor(
            full_cmd=full_cmd,
            cmd_name=cmd_name,
            prompt=prompt,
            log_file=log_file,
            wall_timeout=wall_timeout,
            inactivity_timeout=inactivity_timeout,
            cwd=cwd,
            env=run_env,
            on_output=on_output,
            on_activity=on_activity,
            start_time=start_time,
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
            )

        if result.success:
            print(
                f"[claude-interactive] Command '{cmd_name}' succeeded",
                file=sys.stderr,
            )

        return MonitoredResult(
            returncode=result.returncode,
            output=output,
            cmd_used=cmd_name,
            cmd_index=0,
            was_retry=False,
        )

    def _make_watcher(
        self, cwd: Optional[Path]
    ) -> "Optional[SessionTranscriptWatcher]":
        """Construct the JSONL transcript watcher for this run.

        Returns ``None`` when ``cwd`` is unknown (the munged-cwd directory cannot
        be derived without it) so the runner degrades to PTY-output-only mode.
        Overridable in tests to inject a fake watcher.
        """
        if cwd is None:
            return None
        try:
            return SessionTranscriptWatcher(cwd=cwd, session_id=self._session_id)
        except Exception:
            return None

    def _run_single_with_monitor(
        self,
        full_cmd: List[str],
        cmd_name: str,
        prompt: str,
        log_file: Optional[Path],
        wall_timeout: Optional[int],
        inactivity_timeout: int,
        cwd: Optional[Path],
        env: Optional[Dict[str, str]],
        on_output: Optional[Callable[[str], None]],
        on_activity: Optional[Callable[[], None]],
        start_time: float,
    ) -> "_SingleRunResult":
        """Spawn the PTY, run the supervisor loop, and reclaim everything.

        The supervisor watches two activity sources in lockstep: the PTY output
        (interaction timing) and the session-transcript JSONL (structured
        result).  New JSONL records are converted to stream-json NDJSON — that
        NDJSON, not the raw ANSI PTY stream, is what is returned to and consumed
        by the upstream tracker.  A single turn is considered done when
        :func:`turn_complete` fires (a terminal JSONL record plus the transcript
        having stopped growing past :data:`TURN_SILENCE_WINDOW`), at which point
        the still-running interactive ``claude`` is torn down.  PTY idleness is
        not part of that decision — the TUI keeps re-rendering after the turn
        ends — but the PTY activity clock still feeds the inactivity-hang
        timeout as a separate safety net.

        When no transcript can be located (no ``cwd``, or the file never
        appears), the runner degrades gracefully to returning the raw PTY output
        and exiting on process death — preserving the G1 behavior.

        The ``finally`` block is the heart of the lifecycle guarantee: on every
        exit path (normal / turn-complete / wall / inactivity / exception /
        KeyboardInterrupt) it stops the I/O threads, force-terminates the process
        group, and joins the threads so nothing leaks.
        """
        handle: Any = None
        log_fh = None
        output_buffer: List[str] = []
        ndjson_buffer: List[str] = []
        activity = _ActivityClock()
        pty_clock = _ActivityClock()
        # Degraded-mode (no-transcript) inactivity signal: only meaningful PTY
        # content advances it, so the TUI's cosmetic spinner / footer re-renders
        # cannot keep the inactivity-hang timer fresh when no JSONL is available.
        content_tracker = _MeaningfulContentTracker()
        stop_event = threading.Event()
        feed_thread: Optional[threading.Thread] = None
        drain_thread: Optional[threading.Thread] = None

        watcher = self._make_watcher(cwd)
        if watcher is not None:
            try:
                watcher.snapshot()
            except Exception:
                watcher = None

        def _emit_ndjson(lines: List[str]) -> None:
            """Buffer/log/forward newly-produced stream-json NDJSON lines."""
            for ln in lines:
                ndjson_buffer.append(ln)
                if log_fh is not None:
                    try:
                        log_fh.write(ln + "\n")
                        log_fh.flush()
                    except Exception:
                        pass
                if on_output is not None:
                    try:
                        on_output(ln + "\n")
                    except Exception:
                        pass

        if log_file:
            try:
                log_file.parent.mkdir(parents=True, exist_ok=True)
                log_fh = open(log_file, "a", encoding="utf-8")
                log_fh.write(f"\n=== Starting: {' '.join(full_cmd)} ===\n")
                log_fh.flush()
            except Exception:
                log_fh = None

        try:
            # --- Controlled start: spawn PTY, hold the handle ---
            try:
                handle = self._spawn_pty(full_cmd, cwd, env)
            except Exception as exc:
                msg = f"\n[claude-interactive] Failed to start '{cmd_name}': {exc}\n"
                output_buffer.append(msg)
                if log_fh:
                    log_fh.write(msg)
                    log_fh.flush()
                return _SingleRunResult(
                    returncode=127,
                    output="".join(output_buffer),
                    success=False,
                    should_retry=True,
                )

            # --- Background I/O threads: feed prompt + drain output ---
            # When a watcher is active, the structured result comes from the
            # JSONL; the raw ANSI PTY stream must NOT reach the upstream tracker
            # (it is not JSON), so the drain thread's on_output is suppressed and
            # NDJSON is forwarded from the poll loop instead.
            drain_on_output = None if watcher is not None else on_output

            # The drain thread fills ``output_buffer`` and bumps ``pty_clock``
            # on every PTY read, so the feed thread can observe render timing to
            # decide when the input box is ready instead of guessing a fixed
            # startup delay.
            feed_thread = threading.Thread(
                target=self._feed_prompt,
                args=(handle, prompt, output_buffer, pty_clock),
                name="claude-interactive-feed",
                daemon=True,
            )
            feed_thread.start()

            drain_thread = threading.Thread(
                target=self._drain_pty,
                args=(
                    handle,
                    activity,
                    stop_event,
                    output_buffer,
                    drain_on_output,
                    on_activity,
                    log_fh if watcher is None else None,
                    pty_clock,
                ),
                name="claude-interactive-drain",
                daemon=True,
            )
            drain_thread.start()

            turn_done = False

            # --- Supervisor loop: composite PTY + JSONL activity / turn-end ---
            while _safe_isalive(handle):
                now = time.time()

                if wall_timeout and (now - start_time) > wall_timeout:
                    msg = f"\n[claude-interactive] Wall timeout ({wall_timeout}s) exceeded\n"
                    output_buffer.append(msg)
                    if log_fh:
                        log_fh.write(msg)
                        log_fh.flush()
                    return _SingleRunResult(
                        returncode=124,
                        output=self._compose_output_with_limit_marker(
                            ndjson_buffer, output_buffer, 124, cmd_name, log_fh
                        ),
                        success=False,
                        should_retry=True,
                    )

                # --- Locate + tail the session transcript JSONL ---
                if watcher is not None:
                    try:
                        if watcher.path is None:
                            watcher.locate()
                        if watcher.path is not None:
                            new_lines = watcher.poll()
                            if new_lines:
                                _emit_ndjson(new_lines)
                                activity.update()
                                if on_activity is not None:
                                    try:
                                        on_activity()
                                    except Exception:
                                        pass
                            # Turn-end is decided by the transcript alone: a
                            # terminal last record plus the JSONL having stopped
                            # growing past the conservative window.  PTY silence
                            # is intentionally NOT required — the interactive TUI
                            # re-renders its footer/spinner forever after the
                            # turn ends, so gating on PTY idleness would hang the
                            # loop on a finished turn.
                            check_now = time.time()
                            jsonl_idle = (
                                check_now - watcher.write_activity()
                            ) > TURN_SILENCE_WINDOW
                            if turn_complete(
                                jsonl_idle, watcher.last_meaningful_record
                            ):
                                turn_done = True
                                break
                    except Exception:
                        # Watcher hiccups must never break the supervisor loop.
                        pass

                # Inactivity / hang detection: once the session transcript has
                # been located, the ONLY meaningful progress signal is JSONL
                # write activity — NOT the PTY clock.  The interactive TUI
                # re-renders an animated spinner / footer on the PTY roughly once
                # per second while a model request is in flight, *including while
                # that request is stalled mid-turn*, so ``activity.last()`` (which
                # ``_drain_pty`` bumps on every non-empty PTY read) stays pinned
                # near ``now`` and could never let ``inactive_time`` cross the
                # threshold.  Genuine forward progress always lands as new
                # transcript records, so we key the hang clock off
                # ``watcher.write_activity()`` (the transcript mtime, which also
                # catches partial writes the line cursor has not yet consumed)
                # alone.  This makes a stalled model call — TUI spinner still
                # animating but no new JSONL records — trip the inactivity timeout
                # exactly like the print-mode runner.
                #
                # Degraded mode — no transcript located yet (startup) or no
                # watcher at all (no ``cwd`` / watcher construction failed).  The
                # raw PTY ``_ActivityClock`` is unusable here for the same reason
                # as above: the TUI's per-second spinner / footer re-render keeps
                # it pinned near ``now`` even on a finished or stalled turn, so it
                # could never let ``inactive_time`` cross the threshold.  With
                # ``wall_timeout=None`` (what ``LLMCaller`` passes) and an
                # interactive ``claude`` that never self-exits after a turn, the
                # supervisor loop would otherwise spin forever (turn_complete is
                # gated on a watcher, so it is skipped here too).  Key the hang
                # clock off ``_MeaningfulContentTracker`` instead: it advances
                # only when genuinely new printable PTY content appears, which the
                # cosmetic re-render noise cannot fake.  A startup hang (no
                # meaningful output ever), a stalled turn, and a finished turn all
                # then trip the inactivity timeout and return control rather than
                # hanging indefinitely.
                if watcher is not None and watcher.path is not None:
                    last_act = watcher.write_activity()
                else:
                    content_tracker.scan(output_buffer)
                    last_act = content_tracker.clock.last()
                inactive_time = now - last_act
                if inactive_time > inactivity_timeout:
                    msg = (
                        f"\n[claude-interactive] Hang detected - inactivity timeout "
                        f"({inactivity_timeout}s) - no activity for {int(inactive_time)}s\n"
                    )
                    output_buffer.append(msg)
                    if log_fh:
                        log_fh.write(msg)
                        log_fh.flush()
                    return _SingleRunResult(
                        returncode=124,
                        output=self._compose_output_with_limit_marker(
                            ndjson_buffer, output_buffer, 124, cmd_name, log_fh
                        ),
                        success=False,
                        should_retry=True,
                    )

                time.sleep(_POLL_INTERVAL)

            # --- Exit: let the drain thread flush, then drain remaining JSONL ---
            if not turn_done and drain_thread is not None:
                drain_thread.join(timeout=5)

            if watcher is not None and watcher.path is not None:
                try:
                    _emit_ndjson(watcher.poll())
                except Exception:
                    pass

            # On a completed turn the interactive process is still alive (it
            # returned to the input box); synthesize the terminal result line
            # and report success.  On a process-death exit, use its exit code.
            if ndjson_buffer:
                if watcher is not None:
                    try:
                        _emit_ndjson([watcher.synthesize_result()])
                    except Exception:
                        pass
                returncode = 0 if turn_done else self._exit_code(handle)
            else:
                returncode = self._exit_code(handle)
                # An active watcher that never located its transcript and produced
                # no structured output means the interactive launch never really
                # started writing a session — surface a marker so the JSONL-state
                # signal reaches detect_infra_error as a STARTUP_FAILURE.  A
                # deliberately watcher-less run (no cwd) is a supported degraded
                # mode, not a startup failure, so it is excluded here.
                if watcher is not None and watcher.path is None:
                    msg = (
                        "\n[claude-interactive] session transcript never created\n"
                    )
                    output_buffer.append(msg)
                    if log_fh:
                        try:
                            log_fh.write(msg)
                            log_fh.flush()
                        except Exception:
                            pass

            output = self._compose_output(ndjson_buffer, output_buffer)

            # Usage-limit detection must draw on the combined PTY output AND the
            # JSONL-derived NDJSON.  In interactive mode the TUI renders a
            # usage/rate-limit message in the PTY stream (captured in
            # output_buffer), NOT in the session-transcript JSONL — so once any
            # assistant line has been written, ``output`` (which _compose_output
            # reduces to the NDJSON alone) no longer carries the limit keywords.
            # Scanning the raw PTY buffer too keeps the limit detectable even
            # after NDJSON has been captured; the appended marker below then
            # carries the keyword forward into ``output`` so the subsequent
            # detect_infra_error call (which only sees the returned output) still
            # classifies this as USAGE_LIMIT and LLMCaller rotates agents.
            scan_text = "".join(output_buffer)
            if ndjson_buffer:
                scan_text += "\n" + "\n".join(ndjson_buffer)

            if self._detect_usage_limit(returncode, scan_text):
                msg = f"\n[claude-interactive] Usage limit detected for '{cmd_name}'\n"
                output += msg
                if log_fh:
                    log_fh.write(msg)
                    log_fh.flush()
                return _SingleRunResult(
                    returncode=returncode,
                    output=output,
                    success=False,
                    should_retry=True,
                )

            return _SingleRunResult(
                returncode=returncode,
                output=output,
                success=returncode == 0,
                should_retry=False,
            )

        except KeyboardInterrupt:
            # Ctrl+C: preserve partial output, mark interrupted, re-handled
            # upstream.  Termination/reap happens in the finally block.
            return _SingleRunResult(
                returncode=-2,
                output=self._compose_output(ndjson_buffer, output_buffer),
                success=False,
                should_retry=False,
                interrupted=True,
            )

        except Exception as exc:  # noqa: BLE001
            msg = f"\n[claude-interactive] Error running '{cmd_name}': {exc}\n"
            output_buffer.append(msg)
            return _SingleRunResult(
                returncode=1,
                output=self._compose_output(ndjson_buffer, output_buffer),
                success=False,
                should_retry=False,
            )

        finally:
            # Guaranteed reclamation on every exit path: stop I/O threads,
            # force-kill the process group, join threads — no orphans, no
            # lingering threads.
            stop_event.set()
            self._terminate(handle)
            if feed_thread is not None:
                try:
                    feed_thread.join(timeout=5)
                except Exception:
                    pass
            if drain_thread is not None:
                try:
                    drain_thread.join(timeout=5)
                except Exception:
                    pass
            if log_fh is not None:
                try:
                    log_fh.close()
                except Exception:
                    pass

    @staticmethod
    def _compose_output(
        ndjson_buffer: List[str], output_buffer: List[str]
    ) -> str:
        """Pick the output payload: stream-json NDJSON when present, else raw PTY.

        When the transcript watcher produced NDJSON lines, those are what the
        upstream tracker must parse — return them joined by newlines.  When no
        NDJSON was captured (no watcher / file never located), fall back to the
        raw PTY capture so the runner still surfaces *something* (the G1
        behavior, plus any ``[claude-interactive] ...`` status lines appended to
        the raw buffer).
        """
        if ndjson_buffer:
            return "\n".join(ndjson_buffer)
        return "".join(output_buffer)

    def _compose_output_with_limit_marker(
        self,
        ndjson_buffer: List[str],
        output_buffer: List[str],
        returncode: int,
        cmd_name: str,
        log_fh: Optional[Any] = None,
    ) -> str:
        """Compose the output payload, appending a usage-limit marker if present.

        ``_compose_output`` reduces the result to the NDJSON alone once any
        assistant line was captured, discarding the raw PTY ``output_buffer``
        where the interactive TUI renders a usage/rate-limit message (the
        transcript JSONL never carries that keyword).  On every exit path —
        including the wall- and inactivity-timeout early returns where the
        usage-limited process is left alive at the input box with no terminal
        JSONL record — we therefore scan the *raw* PTY buffer (plus the NDJSON)
        for the limit keywords and, on a hit, append the
        ``[claude-interactive] Usage limit detected`` marker so the keyword
        survives into the returned ``output``.  This lets the downstream
        ``detect_infra_error`` classify the failure as ``USAGE_LIMIT`` (checked
        before timeout) instead of masking it as a generic 30-minute ``TIMEOUT``.
        """
        output = self._compose_output(ndjson_buffer, output_buffer)
        scan_text = "".join(output_buffer)
        if ndjson_buffer:
            scan_text += "\n" + "\n".join(ndjson_buffer)
        if self._detect_usage_limit(returncode, scan_text):
            msg = f"\n[claude-interactive] Usage limit detected for '{cmd_name}'\n"
            output += msg
            if log_fh:
                try:
                    log_fh.write(msg)
                    log_fh.flush()
                except Exception:
                    pass
        return output

    @staticmethod
    def _exit_code(handle: Any) -> int:
        """Best-effort exit code from a finished ``pexpect.spawn`` handle.

        Prefers the process exit status; falls back to ``128 + signal`` when
        the child was signalled, or ``0``/``1`` when neither is available.
        """
        try:
            status = getattr(handle, "exitstatus", None)
            if status is not None:
                return int(status)
            sig = getattr(handle, "signalstatus", None)
            if sig:
                return 128 + int(sig)
        except Exception:
            pass
        # Unknown — treat a still-alive handle as failure, else success.
        return 1 if _safe_isalive(handle) else 0

    # ------------------------------------------------------------------
    # detect_infra_error — interactive-mode classification (PTY + JSONL state)
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_usage_limit(returncode: int, output: str) -> bool:
        """Scan the output tail for usage/rate-limit indicators (failures only)."""
        if returncode == 0:
            return False
        combined = output or ""
        tail = combined[-3000:].lower()
        lines = combined.split("\n")
        last_lines = "\n".join(lines[-20:]).lower()
        for keyword in USAGE_LIMIT_KEYWORDS:
            if keyword in tail or keyword in last_lines:
                return True
        return False

    @staticmethod
    def _detect_startup_failure(output: str) -> bool:
        """Whether the output carries an interactive-launch startup-failure marker.

        Matches the markers ``_run_single_with_monitor`` emits when the PTY child
        fails to spawn or its session transcript is never written
        (:data:`STARTUP_FAILURE_KEYWORDS`).  The signal therefore comes from the
        PTY output buffer + the JSONL location state, not print-mode stdout.
        """
        text = (output or "").lower()
        return any(kw in text for kw in STARTUP_FAILURE_KEYWORDS)

    def detect_infra_error(
        self,
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> InfraErrorType:
        """Classify infrastructure errors for :class:`LLMCaller` rotation.

        Interactive-mode rewrite of the print-mode classifier, drawing on PTY
        output + JSONL transcript state rather than print-mode stdout:

        * ``returncode == 0`` → ``NONE`` (a successful turn is never an infra
          error, even if the transcript happens to mention a limit keyword).
        * usage / rate-limit keywords in the output tail → ``USAGE_LIMIT``
          (checked before timeout so it wins on a synthetic 124 that also
          carries a limit message, matching the print-mode precedence).
        * ``returncode == 124`` → ``TIMEOUT`` — the canonical signal the
          supervisor synthesizes for a wall-timeout, an inactivity hang, or a
          ``timeout(1)`` breach (``HANG`` is expressed via this synthetic 124,
          so it is never returned directly here).
        * a PTY startup failure / session transcript that was never created →
          ``STARTUP_FAILURE``.
        * otherwise → ``NONE`` (an ordinary task failure, not warranting
          agent rotation).
        """
        if returncode == 0:
            return InfraErrorType.NONE
        combined = (stdout or "") + (stderr or "")
        if self._detect_usage_limit(returncode, combined):
            return InfraErrorType.USAGE_LIMIT
        if returncode == 124:
            return InfraErrorType.TIMEOUT
        if self._detect_startup_failure(combined):
            return InfraErrorType.STARTUP_FAILURE
        return InfraErrorType.NONE


# ---------------------------------------------------------------------------
# Result dataclasses (aligned with claude_runner / codex_runner)
# ---------------------------------------------------------------------------


@dataclass
class MonitoredResult:
    """Result from :meth:`ClaudeInteractiveRunner.run_with_monitor`."""

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
    """Internal result from a single monitored PTY run."""

    returncode: int
    output: str
    success: bool
    should_retry: bool
    interrupted: bool = False
    stderr_tail: str = ""
