"""AuthProvider ABC, OwnerIdentity, and ProviderChain composition.

These are the A-layer's vocabulary, independent of any concrete authentication
ceremony or transport:

- :class:`OwnerIdentity` is the resolved result handed to the request handler —
  a stable internal ``owner_id`` plus a human display name. Authorization
  filtering downstream keys on ``owner_id`` only; it is decoupled from every
  provider's external authentication identifier (local username, OIDC
  issuer+sub, proxy-header email), matching the persistence layer's design.
- :class:`AuthProvider` is the single "resolve the current owner" interface
  (``resolve_owner(request) -> OwnerIdentity | None``). Concrete providers
  (local password, OIDC, proxy-header) implement it; ``None`` means "this
  provider cannot identify the requester" — never "anonymous is allowed".
- :class:`ProviderChain` tries enabled providers in order and returns the first
  successful resolution, so multiple providers can coexist (e.g. local + a
  reverse-proxy header) while every request still resolves to exactly one
  owner identity.

``resolve_owner`` takes a *request-like* object (anything exposing ``.cookies``
and ``.headers`` mappings, e.g. a Starlette/FastAPI ``Request``). The base
package never imports FastAPI, so providers stay unit-testable with a tiny fake
request.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, List, Optional


@dataclass(frozen=True)
class OwnerIdentity:
    """The authenticated owner of a request.

    ``owner_id`` is the stable internal primary key (see
    :class:`tianluo.server.persistence.Owner`); it is what authorization filters
    on. ``display_name`` is for UI only. ``provider`` records which provider
    resolved the identity (diagnostics, never authorization). ``is_admin``
    reflects the owner record's admin flag.
    """

    owner_id: str
    display_name: Optional[str] = None
    provider: str = ""
    is_admin: bool = False


class AuthProvider(ABC):
    """The unified 'resolve the current owner' interface (A-layer).

    A provider's ``name`` is the persistence-layer ``provider`` discriminator
    used when binding an external identity (``(provider, external_id)``), so it
    must match the value passed to ``Store.link_identity`` / the daemon never
    touches this layer. ``enabled`` lets the registry skip a configured-but-off
    provider (e.g. OIDC / proxy-header default to disabled) without removing it
    from the chain construction code.
    """

    #: The provider discriminator, also used as the identity-binding ``provider``.
    name: str = "base"
    #: Whether this provider participates in resolution. Disabled providers are
    #: dropped by :class:`ProviderChain` so they never silently trust input.
    enabled: bool = True

    @abstractmethod
    def resolve_owner(self, request: Any) -> Optional[OwnerIdentity]:
        """Resolve ``request`` to an :class:`OwnerIdentity`, or ``None``.

        ``None`` means this provider could not authenticate the requester; it
        does **not** authorize anonymous access. The chain/dependency layer
        turns a fully-``None`` resolution into a 401 (fail-closed).
        """
        raise NotImplementedError


class ProviderChain:
    """Ordered composition of :class:`AuthProvider` s.

    Construction drops any provider whose ``enabled`` is false, so a disabled
    OIDC / proxy-header seam never participates. ``resolve_owner`` returns the
    first provider's non-``None`` result. An empty chain is falsy, which the
    registry uses to enforce fail-closed (refuse to serve when nothing can
    authenticate).
    """

    def __init__(self, providers: List[AuthProvider]):
        self._providers: List[AuthProvider] = [
            p for p in providers if getattr(p, "enabled", True)
        ]

    @property
    def providers(self) -> List[AuthProvider]:
        return list(self._providers)

    def resolve_owner(self, request: Any) -> Optional[OwnerIdentity]:
        for provider in self._providers:
            identity = provider.resolve_owner(request)
            if identity is not None:
                return identity
        return None

    def __bool__(self) -> bool:
        return bool(self._providers)

    def __len__(self) -> int:
        return len(self._providers)
