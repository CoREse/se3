"""Login rate limiting / lockout — brute-force defense for password auth.

Tracks recent *failed* login attempts per key (the login username, optionally
combined with a client identifier by the caller). After ``max_failures``
failures inside ``window_seconds``, the key is locked for ``lockout_seconds``;
attempts while locked are rejected without even consulting the password store.
A successful login clears the key's counter.

This is an in-memory, thread-safe defense intended to blunt online guessing; it
is intentionally simple (no persistence — a restart resets counters, which is
acceptable since it only ever *relaxes*, never strengthens, an attacker's
position, and the credential store itself uses a slow hash).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List


@dataclass
class RateLimitConfig:
    """Tunables for :class:`LoginRateLimiter`."""

    #: Failures within ``window_seconds`` before the key locks.
    max_failures: int = 5
    #: How long a key stays locked once tripped.
    lockout_seconds: float = 300.0
    #: Sliding window over which failures accumulate; older ones are forgotten.
    window_seconds: float = 900.0


@dataclass
class _Entry:
    failures: List[float] = field(default_factory=list)
    locked_until: float = 0.0


class LoginRateLimited(Exception):
    """Raised by an auth provider when a login key is currently locked out.

    ``retry_after`` is the number of seconds the caller should wait (suitable
    for a ``Retry-After`` header / HTTP 429).
    """

    def __init__(self, retry_after: float):
        super().__init__(f"too many failed attempts; retry after {retry_after:.0f}s")
        self.retry_after = retry_after


class LoginRateLimiter:
    """Per-key failure counter with a lockout window. Thread-safe."""

    def __init__(
        self,
        config: RateLimitConfig | None = None,
        *,
        now: Callable[[], float] = time.time,
    ):
        self._config = config or RateLimitConfig()
        self._now = now
        self._entries: Dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def _prune(self, entry: _Entry, now: float) -> None:
        cutoff = now - self._config.window_seconds
        entry.failures = [t for t in entry.failures if t >= cutoff]

    def is_locked(self, key: str) -> bool:
        now = self._now()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return False
            return now < entry.locked_until

    def retry_after(self, key: str) -> float:
        """Seconds remaining on the lockout for ``key`` (0 if not locked)."""
        now = self._now()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return 0.0
            return max(0.0, entry.locked_until - now)

    def check(self, key: str) -> None:
        """Raise :class:`LoginRateLimited` if ``key`` is currently locked."""
        retry = self.retry_after(key)
        if retry > 0:
            raise LoginRateLimited(retry)

    def record_failure(self, key: str) -> None:
        """Register a failed attempt; trips the lockout once over threshold."""
        now = self._now()
        with self._lock:
            entry = self._entries.setdefault(key, _Entry())
            self._prune(entry, now)
            entry.failures.append(now)
            if len(entry.failures) >= self._config.max_failures:
                entry.locked_until = now + self._config.lockout_seconds

    def record_success(self, key: str) -> None:
        """Clear a key's failure history on a successful login."""
        with self._lock:
            self._entries.pop(key, None)

    def reset(self) -> None:
        """Drop all tracked keys (test helper / admin clear)."""
        with self._lock:
            self._entries.clear()
