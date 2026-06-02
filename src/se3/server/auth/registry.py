"""Provider registry, fail-closed assembly, and the ``require_owner`` dependency.

:func:`build_provider_chain` reads the ``server.auth`` configuration, constructs
the configured providers, and returns a :class:`ProviderChain`. Provider
selection is configuration-driven so an operator can enable/switch providers
without code changes; the default (no config, or empty) is the built-in
:class:`LocalAuthProvider`.

**Fail-closed** is the central guarantee: if the configuration leaves *no*
usable authentication provider (e.g. local explicitly disabled and no other
enabled), the chain is empty and :class:`AuthNotConfigured` is raised. The
server must refuse to serve in that state rather than falling back to the old
identity-unaware "bare" mode.

:func:`make_require_owner` builds the FastAPI dependency that every ``/api/*``
and ``/ws/ui`` handler uses to obtain the current :class:`OwnerIdentity`; with
no resolvable identity it raises HTTP 401 — anonymous access is never admitted.
FastAPI is imported lazily inside the factory so this module (and its tests)
import without the web extra.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Tuple

from .base import AuthProvider, OwnerIdentity, ProviderChain
from .local import LocalAuthProvider
from .oidc import OidcProvider
from .proxy_header import ProxyHeaderProvider
from .ratelimit import LoginRateLimiter
from .session import SessionStore

logger = logging.getLogger(__name__)

#: The default provider used when ``server.auth.providers`` is unset/empty.
DEFAULT_PROVIDER = "local"

#: Recognized provider type discriminators.
PROVIDER_LOCAL = "local"
PROVIDER_OIDC = "oidc"
PROVIDER_PROXY_HEADER = "proxy_header"


class AuthNotConfigured(RuntimeError):
    """Raised when no usable auth provider is configured (fail-closed).

    Signals that the server must refuse to serve rather than fall back to an
    identity-unaware open control plane.
    """


def _normalize_entry(entry: Any) -> Tuple[str, dict]:
    """Normalize a providers-list entry to ``(type, options)``.

    Accepts either a bare type string (``"local"``) or a mapping carrying a
    ``type`` key plus provider options (``{"type": "oidc", "issuer": ...}``).
    """
    if isinstance(entry, str):
        return entry.strip().lower(), {}
    if isinstance(entry, Mapping):
        opts = {k: v for k, v in entry.items() if k != "type"}
        return str(entry.get("type", "")).strip().lower(), opts
    raise ValueError(f"invalid auth provider entry: {entry!r}")


def build_provider_chain(
    auth_config: Optional[Mapping[str, Any]],
    *,
    store: Any,
    sessions: SessionStore,
    rate_limiter: Optional[LoginRateLimiter] = None,
) -> ProviderChain:
    """Assemble the :class:`ProviderChain` from ``server.auth`` config.

    ``auth_config`` is the ``server.auth`` sub-mapping (``None`` ⇒ defaults).
    Recognized keys:

    - ``providers``: ordered list of provider entries (string type or mapping
      with ``type`` + options). Defaults to ``["local"]``.

    Each provider is constructed and kept only if it reports ``enabled``. The
    seams (OIDC / proxy-header) default to disabled and so drop out unless
    explicitly configured. If the resulting chain is empty,
    :class:`AuthNotConfigured` is raised (fail-closed).
    """
    config = dict(auth_config or {})
    entries = config.get("providers")
    if not entries:
        entries = [DEFAULT_PROVIDER]

    providers: list[AuthProvider] = []
    for entry in entries:
        ptype, opts = _normalize_entry(entry)
        if not ptype:
            raise ValueError(f"auth provider entry missing 'type': {entry!r}")
        # An entry may opt itself out without being removed from the list.
        if opts.get("enabled") is False and ptype == PROVIDER_LOCAL:
            logger.info("auth: local provider explicitly disabled by config")
            continue

        provider = _construct_provider(
            ptype, opts, store=store, sessions=sessions, rate_limiter=rate_limiter
        )
        if provider is None:
            logger.warning("auth: unknown provider type %r ignored", ptype)
            continue
        if not getattr(provider, "enabled", True):
            logger.info("auth: provider %r constructed but disabled; skipping", ptype)
            continue
        providers.append(provider)

    chain = ProviderChain(providers)
    if not chain:
        raise AuthNotConfigured(
            "fail-closed: no usable authentication provider is configured. "
            "Refusing to serve an identity-unaware control plane. Enable the "
            "built-in 'local' provider or configure another auth provider."
        )
    logger.info(
        "auth: provider chain assembled (%d): %s",
        len(chain),
        ", ".join(p.name for p in chain.providers),
    )
    return chain


def _construct_provider(
    ptype: str,
    opts: Mapping[str, Any],
    *,
    store: Any,
    sessions: SessionStore,
    rate_limiter: Optional[LoginRateLimiter],
) -> Optional[AuthProvider]:
    if ptype == PROVIDER_LOCAL:
        return LocalAuthProvider(store, sessions, rate_limiter=rate_limiter)
    if ptype == PROVIDER_OIDC:
        return OidcProvider(opts, store, sessions)
    if ptype == PROVIDER_PROXY_HEADER:
        return ProxyHeaderProvider(opts, store)
    return None


def make_require_owner(chain: ProviderChain):
    """Build the FastAPI dependency resolving the current :class:`OwnerIdentity`.

    The returned callable is used as ``Depends(require_owner)`` on protected
    routes. It runs the provider chain against the request and raises HTTP 401
    when nothing resolves — never admitting anonymous access (fail-closed at the
    request boundary, complementing startup fail-closed in
    :func:`build_provider_chain`).
    """
    from fastapi import HTTPException, Request  # deferred: server extra only

    def require_owner(request) -> OwnerIdentity:
        identity = chain.resolve_owner(request)
        if identity is None:
            raise HTTPException(
                status_code=401,
                detail="authentication required",
                headers={"WWW-Authenticate": "Cookie"},
            )
        return identity

    # This module uses ``from __future__ import annotations``, so any inline
    # annotation would be stored as the *string* ``"Request"`` — and FastAPI's
    # ``get_type_hints`` cannot resolve it because the deferred ``Request``
    # import is a local, not a module global. Assigning the real classes to
    # ``__annotations__`` makes FastAPI recognise the parameter as the request
    # object to inject (rather than mis-reading it as a query field).
    require_owner.__annotations__ = {"request": Request, "return": OwnerIdentity}
    return require_owner
