"""Pluggable authentication layer (A-layer) for the multi-tenant server.

This package provides the unified "resolve the current owner" seam for the
human / UI side of the control plane. It is deliberately separate from the
B-layer identity & authorization model (:mod:`tianluo.server.identity` +
:mod:`tianluo.server.persistence`): A-layer decides *who is making this request*
(authentication ceremony + session), while B-layer owns the stable internal
``owner_id`` and the ``machine -> owner`` binding that authorization filters on.

Components:

- :mod:`base`      — the :class:`AuthProvider` ABC (``resolve_owner(request)``),
  the :class:`OwnerIdentity` dataclass, and :class:`ProviderChain`.
- :mod:`session`   — server-side session store with secure cookie attributes,
  and :class:`SessionAuthProvider` (the session-cookie -> owner resolver shared
  by every provider whose login ceremony establishes a session).
- :mod:`local`     — :class:`LocalAuthProvider`, the default built-in
  username + password multi-owner authenticator (no external IdP required).
- :mod:`ratelimit` — login failure counting + lockout (brute-force defense).
- :mod:`registry`  — assembles the provider chain from ``server.auth`` config,
  enforces fail-closed, and exposes the ``require_owner`` FastAPI dependency.
- :mod:`oidc` / :mod:`proxy_header` — optional-provider seams, disabled by
  default (v1 keeps the schema/接缝 open without forcing implementation).

Only :mod:`registry` touches FastAPI, and it does so behind a deferred import,
so the rest of the package (and its tests) load without the ``tianluo[server]``
extra's web dependencies.
"""

from __future__ import annotations

from .base import AuthProvider, OwnerIdentity, ProviderChain
from .local import LocalAuthProvider
from .ratelimit import LoginRateLimited, LoginRateLimiter, RateLimitConfig
from .registry import (
    AuthNotConfigured,
    DEFAULT_PROVIDER,
    build_provider_chain,
    make_require_owner,
)
from .session import CookieConfig, Session, SessionAuthProvider, SessionStore

__all__ = [
    "AuthProvider",
    "OwnerIdentity",
    "ProviderChain",
    "CookieConfig",
    "Session",
    "SessionStore",
    "SessionAuthProvider",
    "LocalAuthProvider",
    "RateLimitConfig",
    "LoginRateLimiter",
    "LoginRateLimited",
    "AuthNotConfigured",
    "DEFAULT_PROVIDER",
    "build_provider_chain",
    "make_require_owner",
]
