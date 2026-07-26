"""OIDC provider seam — architecturally accommodated, disabled by default in v1.

This is a *seam*, not a working OIDC client: it keeps the schema and call path
open so a future delivery can add app-internal social login without reshaping
the auth layer. v1 does not force its implementation (see the multi-tenant
non-goals).

Key decisions captured here so the seam is not "dug shut":

- The external identity for an OIDC login is ``issuer + "|" + sub`` — the only
  globally stable, non-reassignable subject identifier. It is mounted onto an
  ``owner_id`` exactly like any other ``(provider, external_id)`` binding
  (provider = :data:`PROVIDER_OIDC`), so switching to / adding OIDC later is the
  additive "hang another binding" path, with daemon keys untouched.
- A real implementation MUST perform standard ``state`` (CSRF) and ``nonce``
  (replay/association) validation on the callback, and set session cookies with
  the secure attributes already provided by :class:`CookieConfig`.

Until configured + implemented the provider reports ``enabled = False`` and
``resolve_owner`` returns ``None``, so it never interferes with the default
local path and never trusts anything.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from .session import SessionAuthProvider, SessionStore

logger = logging.getLogger(__name__)

#: Identity-binding discriminator for OIDC subjects.
PROVIDER_OIDC = "oidc"


def oidc_external_id(issuer: str, subject: str) -> str:
    """Compose the stable OIDC external id from ``issuer`` and ``sub``.

    ``issuer`` scopes ``sub`` (a ``sub`` is only unique within its issuer), so
    the binding key is ``"{issuer}|{sub}"``. This is the value handed to
    ``Store.link_identity(owner_id, PROVIDER_OIDC, oidc_external_id(...))``.
    """
    return f"{issuer}|{subject}"


class OidcProvider(SessionAuthProvider):
    """Disabled-by-default OIDC seam.

    Construction validates that the minimum config is present
    (``issuer`` / ``client_id`` / ``client_secret`` / ``redirect_uri``); if any
    is missing the provider stays disabled rather than half-trusting a partial
    configuration. Even when fully configured, the login ceremony is not
    implemented in v1 and :meth:`begin_login` / :meth:`complete_login` raise
    :class:`NotImplementedError`.
    """

    name = PROVIDER_OIDC

    _REQUIRED_KEYS = ("issuer", "client_id", "client_secret", "redirect_uri")

    def __init__(
        self,
        config: Optional[Mapping[str, Any]],
        store: Any,
        sessions: SessionStore,
    ):
        super().__init__(store, sessions)
        self._config = dict(config or {})
        explicitly_enabled = bool(self._config.get("enabled", False))
        fully_configured = all(self._config.get(k) for k in self._REQUIRED_KEYS)
        # Only enabled when the operator opts in AND the config is complete.
        self.enabled = explicitly_enabled and fully_configured
        if explicitly_enabled and not fully_configured:
            missing = [k for k in self._REQUIRED_KEYS if not self._config.get(k)]
            logger.warning(
                "oidc provider enabled in config but incomplete (missing %s); "
                "staying disabled (fail-closed)",
                ", ".join(missing),
            )

    def resolve_owner(self, request: Any):
        # When disabled, never look at the request at all.
        if not self.enabled:
            return None
        # When enabled, resolution is the standard session-cookie path: a
        # completed OIDC login mints a session like any other ceremony.
        return super().resolve_owner(request)

    def begin_login(self, *, state: str, nonce: str) -> str:
        """Return the authorization-endpoint redirect URL (NOT implemented in v1).

        A real implementation builds the authorize URL embedding ``state`` and
        ``nonce`` and stores them server-side for callback validation.
        """
        raise NotImplementedError(
            "OIDC login ceremony is a v1 seam; not implemented. Wire standard "
            "state/nonce validation here when enabling OIDC."
        )

    def complete_login(self, *, code: str, state: str) -> Optional[str]:
        """Handle the OIDC callback and return a session id (NOT implemented in v1).

        A real implementation validates ``state`` against the stored value,
        exchanges ``code`` for tokens, validates the id-token ``nonce`` and
        signature, derives :func:`oidc_external_id`, resolves/links the owner,
        and mints a session.
        """
        raise NotImplementedError(
            "OIDC callback handling is a v1 seam; not implemented. Validate "
            "state/nonce and bind issuer+sub as the external_id when enabling."
        )
