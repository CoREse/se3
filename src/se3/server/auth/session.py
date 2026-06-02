"""Server-side session store + the session-cookie owner resolver.

The UI session credential is deliberately a *different* exposure surface from a
daemon key: a session id lives only in the user's browser cookie and the
server's in-memory :class:`SessionStore` (never persisted, never in the sqlite
``daemon_keys`` table), while a daemon key is minted at a separate endpoint,
hashed into persistence, and carried in the daemon HELLO. They share neither
storage nor endpoint.

Security properties:

- **High entropy** — a session id is a 256-bit random token
  (:func:`se3.server.crypto.generate_token`); guessing one is infeasible, so a
  fast hash for storage is sufficient (same rationale as daemon keys).
- **Hashed at rest** — only the SHA-256 hash of a session id is kept in memory,
  so a memory dump never yields a live session id directly.
- **Constant-time validation** — lookup matches the presented id's hash against
  the stored hash via :func:`se3.server.crypto.const_eq`, avoiding a timing
  oracle on the compare.
- **Expiry** — every session carries an absolute ``expires_at``; resolution
  rejects (and drops) expired sessions, and a lightweight sweep prunes them.

Secure cookie attributes (``HttpOnly`` / ``SameSite`` / ``Secure`` / ``Path`` /
``Max-Age``) are described by :class:`CookieConfig` and honoured when the app
layer sets the cookie.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from .. import crypto
from .base import AuthProvider, OwnerIdentity

logger = logging.getLogger(__name__)

#: Default session lifetime (seconds). 12h balances "don't re-login constantly"
#: against bounding the window of a stolen cookie.
DEFAULT_SESSION_TTL = 12 * 60 * 60

#: Token prefix so a leaked log line (should one ever slip through) is at least
#: recognizable as a session id rather than mistaken for a daemon key.
_SESSION_PREFIX = "ses"


@dataclass
class CookieConfig:
    """Secure-cookie attributes for the UI session cookie.

    ``secure`` defaults to ``True`` because the deployment terminates TLS at a
    reverse proxy (see the multi-tenant threat model); an operator running a
    plain-HTTP localhost dev server can flip it off. ``same_site='lax'`` blocks
    cross-site POST CSRF on the cookie while still allowing top-level
    navigation logins.
    """

    name: str = "se3_session"
    http_only: bool = True
    same_site: str = "lax"
    secure: bool = True
    path: str = "/"
    max_age: int = DEFAULT_SESSION_TTL


@dataclass
class Session:
    """A live server-side session. ``id_hash`` is the stored secret material;
    the plaintext id is returned only once from :meth:`SessionStore.create`."""

    id_hash: str
    owner_id: str
    created_at: float
    expires_at: float

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at


def read_cookie(request: Any, name: str) -> Optional[str]:
    """Best-effort read of a cookie from a request-like object.

    Accepts anything exposing a ``.cookies`` mapping (Starlette/FastAPI
    ``Request``, or a test fake). Returns ``None`` rather than raising on a
    missing attribute / malformed mapping, so resolution stays fail-closed.
    """
    cookies = getattr(request, "cookies", None)
    if not cookies:
        return None
    try:
        value = cookies.get(name)
    except Exception:  # pragma: no cover - defensive against odd mappings
        return None
    return value or None


class SessionStore:
    """In-memory, hashed, expiring session table.

    Thread-safe (the server services requests across an event loop + worker
    threads). Sessions are keyed by the session id's hash, so the table never
    holds a plaintext id.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_SESSION_TTL,
        cookie_config: Optional[CookieConfig] = None,
        now: Callable[[], float] = time.time,
    ):
        self._ttl = ttl_seconds
        self._cookie = cookie_config or CookieConfig(max_age=ttl_seconds)
        self._now = now
        self._sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()

    @property
    def cookie_config(self) -> CookieConfig:
        return self._cookie

    def create(self, owner_id: str) -> Tuple[str, Session]:
        """Mint a new session for ``owner_id``.

        Returns ``(session_id_plaintext, Session)``. The plaintext is set as the
        cookie value and never stored; only its hash lives in the table.
        """
        plaintext, id_hash = crypto.generate_token(_SESSION_PREFIX)
        now = self._now()
        session = Session(
            id_hash=id_hash,
            owner_id=owner_id,
            created_at=now,
            expires_at=now + self._ttl,
        )
        with self._lock:
            self._sessions[id_hash] = session
        return plaintext, session

    def resolve(self, session_id: Optional[str]) -> Optional[Session]:
        """Resolve a session-id plaintext to a live :class:`Session`.

        Hashes the presented id, looks it up, and confirms the match with a
        constant-time compare before checking expiry. Expired sessions are
        evicted in passing. Returns ``None`` for missing / unknown / expired.
        """
        if not session_id:
            return None
        presented = crypto.token_hash(session_id)
        now = self._now()
        with self._lock:
            session = self._sessions.get(presented)
            if session is None:
                return None
            if not crypto.const_eq(presented, session.id_hash):
                return None  # pragma: no cover - dict key already equals hash
            if session.is_expired(now):
                self._sessions.pop(presented, None)
                return None
            return session

    def destroy(self, session_id: Optional[str]) -> bool:
        """Invalidate a session by its plaintext id (logout). Idempotent."""
        if not session_id:
            return False
        presented = crypto.token_hash(session_id)
        with self._lock:
            return self._sessions.pop(presented, None) is not None

    def destroy_owner(self, owner_id: str) -> int:
        """Invalidate every session of ``owner_id`` (e.g. on disable). Returns count."""
        with self._lock:
            doomed = [h for h, s in self._sessions.items() if s.owner_id == owner_id]
            for h in doomed:
                self._sessions.pop(h, None)
            return len(doomed)

    def sweep_expired(self) -> int:
        """Prune expired sessions; returns how many were removed."""
        now = self._now()
        with self._lock:
            doomed = [h for h, s in self._sessions.items() if s.is_expired(now)]
            for h in doomed:
                self._sessions.pop(h, None)
            return len(doomed)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


class SessionAuthProvider(AuthProvider):
    """Resolves the current owner from the session cookie.

    Base for every provider whose login ceremony establishes a server-side
    session (the default :class:`~se3.server.auth.local.LocalAuthProvider`, and
    the OIDC seam). Resolution is uniform regardless of which ceremony minted
    the session — a session is just an ``owner_id`` reference — so subclasses
    only add their ceremony, not their own resolution path.
    """

    name = "session"

    def __init__(self, store: Any, sessions: SessionStore):
        self._store = store
        self._sessions = sessions

    @property
    def sessions(self) -> SessionStore:
        return self._sessions

    @property
    def store(self) -> Any:
        return self._store

    def resolve_owner(self, request: Any) -> Optional[OwnerIdentity]:
        session_id = read_cookie(request, self._sessions.cookie_config.name)
        session = self._sessions.resolve(session_id)
        if session is None:
            return None
        owner = self._store.get_owner(session.owner_id)
        if owner is None:
            # Owner deleted out from under a live session: fail closed and drop it.
            self._sessions.destroy(session_id)
            return None
        return OwnerIdentity(
            owner_id=owner.owner_id,
            display_name=owner.display_name,
            provider=self.name,
            is_admin=owner.is_admin,
        )

    def logout(self, request: Any) -> bool:
        """Destroy the session referenced by the request cookie. Idempotent."""
        session_id = read_cookie(request, self._sessions.cookie_config.name)
        return self._sessions.destroy(session_id)
