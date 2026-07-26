"""Reverse-proxy trusted-header provider seam — disabled by default in v1.

Architecturally accommodated for deployments fronted by an authenticating
reverse proxy (oauth2-proxy / authelia / Cloudflare Access) that injects the
authenticated user's identity as a request header. v1 does not force its
implementation; this seam keeps the schema/path open and, crucially, **refuses
to trust any header unless a trusted source is explicitly declared**.

Hard security preconditions (the operator's responsibility, surfaced here as a
config gate so the default cannot silently trust client input):

- The reverse proxy MUST strip any client-supplied header of the same name
  before injecting its own, otherwise a client forges the identity header.
- The server MUST be unreachable except through that proxy (no bypass / direct
  connect), otherwise the header arrives unauthenticated.

Because those cannot be verified from inside the process, this provider is
**off by default** and only enables when the operator both sets
``enabled: true`` and declares the trust precondition (``trust_proxy: true``)
plus the header name. Absent that, ``enabled`` is ``False`` and
``resolve_owner`` returns ``None`` even if the header is present — it never
default-trusts an arbitrary header.

The header value (typically a verified email) is treated as the
``(provider, external_id)`` external id (provider = :data:`PROVIDER_PROXY`) and
resolved through the persistence layer; an unbound value resolves to ``None``
(no implicit account creation here).
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from .base import AuthProvider, OwnerIdentity

logger = logging.getLogger(__name__)

#: Identity-binding discriminator for proxy-header identities.
PROVIDER_PROXY = "proxy_header"

#: Conventional default header an authenticating proxy injects.
DEFAULT_HEADER = "x-forwarded-email"


def _read_header(request: Any, name: str) -> Optional[str]:
    headers = getattr(request, "headers", None)
    if not headers:
        return None
    try:
        value = headers.get(name)
    except Exception:  # pragma: no cover - defensive against odd mappings
        return None
    return value or None


class ProxyHeaderProvider(AuthProvider):
    """Disabled-by-default reverse-proxy identity-header provider.

    Enabled only when the operator opts in (``enabled: true``) AND affirms the
    trust precondition (``trust_proxy: true``) AND a header name is configured.
    If asked to enable without affirming trust, it logs a warning and stays
    disabled — fail-closed rather than trusting forgeable input.
    """

    name = PROVIDER_PROXY

    def __init__(self, config: Optional[Mapping[str, Any]], store: Any):
        self._store = store
        self._config = dict(config or {})
        self._header = (self._config.get("header") or DEFAULT_HEADER).lower()

        explicitly_enabled = bool(self._config.get("enabled", False))
        trusted = bool(self._config.get("trust_proxy", False))
        self.enabled = explicitly_enabled and trusted and bool(self._header)
        if explicitly_enabled and not trusted:
            logger.warning(
                "proxy_header provider enabled in config but 'trust_proxy' was "
                "not affirmed; staying disabled. The reverse proxy MUST strip "
                "client-supplied %r and the server MUST be unreachable except "
                "through the proxy before this may be trusted.",
                self._header,
            )

    @property
    def header(self) -> str:
        return self._header

    def resolve_owner(self, request: Any) -> Optional[OwnerIdentity]:
        # Never read the header unless a trusted source was explicitly declared.
        if not self.enabled:
            return None
        external_id = _read_header(request, self._header)
        if not external_id:
            return None
        owner_id = self._store.resolve_owner_by_identity(PROVIDER_PROXY, external_id)
        if not owner_id:
            # Known-good header from the proxy, but no owner bound to it. We do
            # not auto-create owners here (the link UX is a future delivery);
            # the schema path to bind external_id -> owner_id stays open via
            # Store.link_identity / IdentityService.link_identity_to_owner.
            return None
        owner = self._store.get_owner(owner_id)
        if owner is None:
            return None
        return OwnerIdentity(
            owner_id=owner.owner_id,
            display_name=owner.display_name,
            provider=self.name,
            is_admin=owner.is_admin,
        )
