"""Per-message-type wire byte accounting, shared by daemon and server.

Both ends of the daemon↔server↔WebUI link need a cheap, dependency-free way to
answer one question after the traffic-reduction work lands: *where are the bytes
actually going, by message type?* An idle daemon should cost only keepalive-sized
traffic, and an active session should cost traffic on the order of the newly
appended conversation content — not a multiple of the total flow / issue / bundle
size. A running counter keyed by message type is what makes that verifiable at
runtime (via a log line or the daemon's status output) and guards against
regressions later.

This module deliberately has **no** dependency on ``se3.server`` (the core /
server dependency isolation runs one-way: server may import daemon, never the
reverse) and pulls in nothing beyond the stdlib, so the core package can use it
without dragging in the server extra.
"""

from __future__ import annotations

import threading
from typing import Dict


class WireMetrics:
    """Thread-safe, in-process, per-message-type sent-byte accumulator.

    A single instance is held by each end (one in the daemon client, one in the
    server relay). :meth:`record` is called on every send path with the message
    type and the encoded frame's byte length; :meth:`snapshot` returns a plain
    ``{msg_type: total_bytes}`` dict (plus a synthetic ``__total__`` /
    ``__count__`` roll-up) for logging or status output.

    Accesses are guarded by a lock because the daemon send path and background
    tasks (ping/pong, history pushes) can run on different threads / tasks; the
    counters are cheap integer adds, so lock contention is negligible.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bytes: Dict[str, int] = {}
        self._counts: Dict[str, int] = {}

    def record(self, msg_type: str, nbytes: int) -> None:
        """Add *nbytes* (and one frame) to the running total for *msg_type*.

        Negative or non-integer *nbytes* are coerced to ``0`` so a bad call site
        can never corrupt the counters or crash the send path — accounting must
        never take down real traffic.
        """
        try:
            n = int(nbytes)
        except (TypeError, ValueError):
            n = 0
        if n < 0:
            n = 0
        key = str(msg_type)
        with self._lock:
            self._bytes[key] = self._bytes.get(key, 0) + n
            self._counts[key] = self._counts.get(key, 0) + 1

    def snapshot(self) -> Dict[str, Dict[str, int]]:
        """Return a point-in-time copy of the accumulated counters.

        The result maps each seen message type to ``{"bytes": …, "count": …}``
        and includes a synthetic ``"__total__"`` roll-up across all types, so a
        caller can report both the per-type breakdown (which link dominates) and
        the grand total in one read. Returning a fresh dict keeps callers from
        mutating internal state.
        """
        with self._lock:
            per_type = {
                key: {"bytes": self._bytes[key], "count": self._counts.get(key, 0)}
                for key in self._bytes
            }
            total_bytes = sum(self._bytes.values())
            total_count = sum(self._counts.values())
        per_type["__total__"] = {"bytes": total_bytes, "count": total_count}
        return per_type

    def reset(self) -> None:
        """Clear all counters. Primarily for tests and per-window sampling."""
        with self._lock:
            self._bytes.clear()
            self._counts.clear()
