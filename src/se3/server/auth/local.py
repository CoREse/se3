"""LocalAuthProvider — the default built-in multi-owner authenticator.

This is the v1-mandatory provider: a self-managed local account system
(username + password + server-side session) that can authenticate many
distinct owners **without depending on any external IdP**. It is what makes
the multi-trust-domain requirement satisfiable out of the box — break-glass is
a single admin subject and cannot stand in for it.

The login ceremony:

1. Rate-limit gate: a locked key (too many recent failures) is refused before
   touching the password store (:class:`~se3.server.auth.ratelimit.LoginRateLimited`).
2. Resolve the owner from the identity binding ``("local", username)``.
3. Verify the supplied password against the stored slow hash
   (:func:`se3.server.crypto.verify_password`). A miss runs a dummy verify so
   the timing of "no such user" matches "wrong password" (mitigates user
   enumeration).
4. On success: clear the failure counter and mint a session.
   On failure: record the failure and return ``None``.

``resolve_owner`` is inherited from :class:`SessionAuthProvider` — once a
session exists, resolution is provider-agnostic.

Credential hygiene: neither the password nor the session id is ever logged.
Only the (non-secret) username and a coarse outcome are logged.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

from .. import crypto
from .base import OwnerIdentity
from .ratelimit import LoginRateLimiter
from .session import SessionAuthProvider, SessionStore

logger = logging.getLogger(__name__)

#: The identity-binding provider discriminator for local accounts. Must match
#: the value used with ``Store.link_identity`` when an owner's local username is
#: registered.
PROVIDER_LOCAL = "local"

#: A throwaway password hashed once and reused only to equalize the timing of a
#: failed login against an unknown username. Computed lazily so a core-only
#: install (no hashing backend) never pays for it at import.
_DUMMY_PASSWORD = "se3-timing-equalizer-not-a-real-password"


class LocalAuthProvider(SessionAuthProvider):
    """Username + password + session authentication over the local store."""

    name = PROVIDER_LOCAL

    def __init__(
        self,
        store: Any,
        sessions: SessionStore,
        *,
        rate_limiter: Optional[LoginRateLimiter] = None,
    ):
        super().__init__(store, sessions)
        self._rate = rate_limiter or LoginRateLimiter()
        self._dummy_hash: Optional[str] = None

    @property
    def rate_limiter(self) -> LoginRateLimiter:
        return self._rate

    def _dummy_verify(self, password: str) -> None:
        """Spend roughly one slow-hash verify on the unknown-user path.

        Best-effort: if no hashing backend is installed the verify is skipped
        (the timing channel is a minor concern relative to never crashing).
        """
        try:
            if self._dummy_hash is None:
                self._dummy_hash = crypto.hash_password(_DUMMY_PASSWORD)
            crypto.verify_password(password, self._dummy_hash)
        except Exception:  # pragma: no cover - backend-absent / best effort
            pass

    def login(
        self, username: str, password: str
    ) -> Optional[Tuple[str, OwnerIdentity]]:
        """Authenticate ``username`` / ``password``.

        Returns ``(session_id, OwnerIdentity)`` on success (the caller sets the
        session cookie), or ``None`` on bad credentials. Raises
        :class:`~se3.server.auth.ratelimit.LoginRateLimited` when the key is
        locked out — the caller maps that to HTTP 429.
        """
        key = (username or "").strip().lower()
        # Gate before consulting the store, so a locked attacker learns nothing.
        self._rate.check(key)

        owner_id = self._store.resolve_owner_by_identity(PROVIDER_LOCAL, username)
        stored_hash = self._store.get_password_hash(owner_id) if owner_id else None

        if not stored_hash:
            # Unknown user / no password set: equalize timing, then fail.
            self._dummy_verify(password)
            self._rate.record_failure(key)
            logger.info("local login failed: unknown user %r", username)
            return None

        if not crypto.verify_password(password, stored_hash):
            self._rate.record_failure(key)
            logger.info("local login failed: bad password for user %r", username)
            return None

        # Success: reset the counter and mint a session. Never log the session id.
        self._rate.record_success(key)
        session_id, _session = self._sessions.create(owner_id)
        owner = self._store.get_owner(owner_id)
        identity = OwnerIdentity(
            owner_id=owner_id,
            display_name=owner.display_name if owner else None,
            provider=self.name,
            is_admin=owner.is_admin if owner else False,
        )
        logger.info("local login ok: user %r -> owner %s", username, owner_id)
        return session_id, identity
