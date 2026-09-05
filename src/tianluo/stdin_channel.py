"""Process-wide funnel for reads of a non-TTY stdin.

WHY this exists: off a terminal, stdin is a pipe the launcher may hold open
indefinitely, so ``sys.stdin.read()`` returns only at EOF. A wait that races
that read against another channel (the web console) therefore cannot simply
abandon it — the abandoned reader stays blocked on the fd forever and eats
whatever the operator types next, which then never reaches the consumer that
asked for it (a gate's choice, a CONFIRM feedback read).

INVARIANT: exactly one reader may own a non-TTY stdin. That owner is the
feeder thread here; it pulls whole lines off the fd into a shared buffer and
never answers a particular wait. Consumers take what they need out of that
buffer — a line, or everything up to EOF — and a consumer that gives up
(because another channel answered first) leaves the bytes it did not take in
the buffer for the next one. Every non-TTY stdin read reachable from a ``luo
run`` goes through this module — the dialog/discovery waits, the gate menus,
the CONFIRM feedback read; nothing on that path may touch the fd itself.

TTY reads are none of this module's business: prompt_toolkit owns the terminal
and its reads are cancellable, so they keep going straight to the terminal.
"""

from __future__ import annotations

import sys
import threading
from typing import Any, List, Optional


class _Pending:
    """Sentinel: the requested answer is not complete yet."""

    _instance: Optional["_Pending"] = None

    def __new__(cls) -> "_Pending":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<stdin PENDING>"

    def __bool__(self) -> bool:
        return False


#: Returned by the bounded reads when the deadline passed with no answer yet.
PENDING = _Pending()


class _SharedStdin:
    """The single owner of a non-TTY stdin fd."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._chunks: List[str] = []
        self._eof = False
        self._thread: Optional[threading.Thread] = None
        self._stream: Any = None
        # Seeded buffers (tests) own the funnel outright: no feeder may be
        # started for them, or it would race the real captured stdin.
        self._feeder_disabled = False

    # -- feeder -----------------------------------------------------------
    def _ensure_feeder_locked(self) -> None:
        stream = sys.stdin
        if self._stream is not stream:
            # A different stdin object than the one being drained (a test
            # substituting a stream, or a re-opened fd): the old feeder speaks
            # for a stream nobody reads any more, so start over on this one.
            self._chunks = []
            self._eof = False
            self._thread = None
            self._stream = stream
            self._feeder_disabled = False
        if self._feeder_disabled:
            return
        if self._eof or (self._thread is not None and self._thread.is_alive()):
            return
        if stream is None:
            self._eof = True
            return

        thread = threading.Thread(
            target=self._feed, args=(stream,),
            name="tianluo-stdin-feeder", daemon=True,
        )
        self._thread = thread
        thread.start()

    def _feed(self, stream: Any) -> None:
        while True:
            try:
                line = stream.readline()
            except Exception:
                # A stream that cannot be read (closed fd, pytest's capture
                # stub) is indistinguishable from one at EOF for every
                # consumer here, and must not leave them waiting forever.
                line = ""
            with self._cond:
                if self._stream is not stream:
                    # Superseded by a newer stdin; this feeder's leftovers
                    # belong to a stream nobody is reading any more.
                    return
                if not line:
                    self._eof = True
                    self._cond.notify_all()
                    return
                self._chunks.append(line)
                self._cond.notify_all()

    # -- consumers --------------------------------------------------------
    def read_all(self, timeout: Optional[float] = None) -> Any:
        """Everything up to EOF, or :data:`PENDING` if *timeout* ran out.

        Returns ``None`` at EOF with nothing buffered — the "no answer" shape
        the terminal readers already use. A ``PENDING`` return consumes
        nothing, so the caller may abandon the read and let a later consumer
        (or a later call to this one) take the bytes.
        """
        with self._cond:
            self._ensure_feeder_locked()
            if not self._eof:
                self._cond.wait_for(lambda: self._eof, timeout=timeout)
            if not self._eof:
                return PENDING
            content = "".join(self._chunks)
            self._chunks = []
            return content if content else None

    def read_line(self, timeout: Optional[float] = None) -> Any:
        """One line (newline stripped), or :data:`PENDING` on timeout.

        ``None`` means EOF with nothing left — the caller's non-interactive
        fallback. A trailing unterminated fragment at EOF is still a line.
        """
        with self._cond:
            self._ensure_feeder_locked()

            def _ready() -> bool:
                return bool(self._chunks) or self._eof

            if not _ready():
                self._cond.wait_for(_ready, timeout=timeout)
            if not _ready():
                return PENDING
            if not self._chunks:
                return None
            line = self._chunks.pop(0)
            return line.rstrip("\r\n")

    def reset(self) -> None:
        """Drop all buffered state (tests; a new stdin in the same process).

        The feeder thread is not joined — it cannot be interrupted mid-read —
        but detaching the stream makes it discard whatever it is holding and
        exit, so it can never hand bytes to the next funnel.
        """
        with self._cond:
            self._chunks = []
            self._eof = False
            self._thread = None
            self._stream = None
            self._feeder_disabled = False
            self._cond.notify_all()


_CHANNEL = _SharedStdin()


def read_all(timeout: Optional[float] = None) -> Any:
    """Read non-TTY stdin up to EOF through the process-wide funnel."""
    return _CHANNEL.read_all(timeout)


def read_line(timeout: Optional[float] = None) -> Any:
    """Read one line of non-TTY stdin through the process-wide funnel."""
    return _CHANNEL.read_line(timeout)


def reset() -> None:
    """Reset the funnel's buffered state."""
    _CHANNEL.reset()


def feed_for_test(text: str, *, eof: bool = True) -> None:
    """Seed the funnel's buffer directly, bypassing the fd.

    Test-only entry point: it lets a test drive the consumers without a real
    pipe, and — because it marks the stream as already owned — keeps the
    feeder from racing pytest's captured stdin.
    """
    with _CHANNEL._cond:  # noqa: SLF001 - deliberate test seam
        _CHANNEL._stream = sys.stdin
        _CHANNEL._thread = None
        _CHANNEL._feeder_disabled = True
        _CHANNEL._chunks = text.splitlines(keepends=True) if text else []
        _CHANNEL._eof = eof
        _CHANNEL._cond.notify_all()


def append_for_test(text: str, *, eof: bool = False) -> None:
    """Append to a seeded buffer, as a live pipe writer would."""
    with _CHANNEL._cond:  # noqa: SLF001 - deliberate test seam
        _CHANNEL._chunks.extend(text.splitlines(keepends=True))
        if eof:
            _CHANNEL._eof = True
        _CHANNEL._cond.notify_all()
