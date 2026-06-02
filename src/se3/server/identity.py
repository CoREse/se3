"""Identity & authorization model: owner resolution and machine binding.

This is the B-layer of the multi-tenant server, independent of which A-layer
auth provider is in use. It sits on top of :class:`se3.server.persistence.Store`
and provides:

- ``resolve_owner_for_key(key)`` — map a daemon key (presented in the WS HELLO)
  to the internal ``owner_id`` it was issued to. The key is hashed before
  lookup, so the plaintext is never compared directly nor stored.
- ``bind_machine`` / ``owner_of_machine`` / ``unbind_machine`` — an in-memory
  ``machine_id -> owner_id`` index. Machine ownership is live state rebuilt
  from daemon reconnects, so it is intentionally NOT persisted; only the
  key→owner fact (which the binding derives from) lives in the Store.
- ``link_identity_to_owner`` — the cross-provider account-linking **trust
  gate** seam. The forward-compatibility requirement is that a future provider
  switch / addition is just hanging another ``(provider, external_id)`` binding
  off an existing ``owner_id``. Linking a *new* external identity to an
  *existing* owner is a sensitive operation (account-takeover risk under public
  exposure), so it is only allowed through an explicit trust gate — an admin
  override, or a claim verified on both sides (e.g. a verified email). This
  layer refuses unverified blind auto-merge by default; the full link UX is
  left to the second provider (this group only keeps the seam open).
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional, Tuple

from .crypto import token_hash
from .persistence import IdentityAlreadyBound, Store

logger = logging.getLogger(__name__)


class UntrustedIdentityLink(Exception):
    """Raised when an external-identity link is attempted without a trust gate.

    Guards against account takeover: linking an unverified external identity to
    an existing owner (e.g. blind email-equality auto-merge) is refused unless
    an admin override or a both-sides-verified claim is supplied.
    """


class IdentityService:
    """Owner/identity resolution plus the live machine→owner binding index."""

    def __init__(self, store: Store):
        self._store = store
        self._machine_owners: Dict[str, str] = {}
        self._lock = threading.Lock()

    # ----- daemon key → owner --------------------------------------------- #

    def resolve_owner_for_key(self, key: Optional[str]) -> Optional[str]:
        """Resolve a daemon key plaintext to its ``owner_id``.

        Hashes the key and delegates to the Store. Returns ``None`` for a
        missing / empty key, or one that does not match any non-revoked stored
        daemon key (the caller then fails the HELLO closed).
        """
        if not key:
            return None
        return self._store.resolve_owner_by_daemon_key(token_hash(key))

    # ----- external identity → owner -------------------------------------- #

    def resolve_owner_by_identity(self, provider: str, external_id: str) -> Optional[str]:
        """Resolve a ``(provider, external_id)`` auth identity to ``owner_id``."""
        return self._store.resolve_owner_by_identity(provider, external_id)

    def link_identity_to_owner(
        self,
        owner_id: str,
        provider: str,
        external_id: str,
        *,
        admin_override: bool = False,
        verified_claim: bool = False,
    ) -> str:
        """Link an external identity to an existing owner — through the trust gate.

        The link is permitted only when the caller passes the trust gate:

        - ``admin_override=True`` — an administrator is performing the link
          manually, or
        - ``verified_claim=True`` — the identity was verified on both sides
          (e.g. a verified-email match), not merely asserted.

        Without either, :class:`UntrustedIdentityLink` is raised. Re-linking an
        identity already bound to *this same* owner is idempotent and bypasses
        the gate. Linking an identity already bound to a *different* owner
        raises :class:`IdentityAlreadyBound`.

        This is a seam: the second provider's full link flow can call here with
        the appropriate gate satisfied. The schema and call path are kept open;
        no automatic unverified merging happens here.
        """
        existing = self._store.resolve_owner_by_identity(provider, external_id)
        if existing is not None:
            if existing == owner_id:
                return owner_id
            raise IdentityAlreadyBound(
                f"identity ({provider!r}, {external_id!r}) is already bound to a "
                f"different owner"
            )
        if not (admin_override or verified_claim):
            raise UntrustedIdentityLink(
                "refusing to link an unverified external identity to an existing "
                "owner; supply an admin override or a both-sides-verified claim "
                "(account-takeover guard)"
            )
        self._store.link_identity(owner_id, provider, external_id)
        return owner_id

    # ----- machine → owner (in-memory live index) ------------------------- #

    def bind_machine(self, machine_id: str, owner_id: str) -> None:
        """Record that ``machine_id`` is owned by ``owner_id`` (live state)."""
        with self._lock:
            self._machine_owners[machine_id] = owner_id

    def owner_of_machine(self, machine_id: str) -> Optional[str]:
        """Return the owner bound to ``machine_id``, or ``None`` if unbound."""
        with self._lock:
            return self._machine_owners.get(machine_id)

    def unbind_machine(self, machine_id: str) -> None:
        """Drop a machine's binding (e.g. on daemon disconnect)."""
        with self._lock:
            self._machine_owners.pop(machine_id, None)

    def machines_for_owner(self, owner_id: str) -> List[str]:
        """Return the machine ids currently bound to ``owner_id``."""
        with self._lock:
            return [m for m, o in self._machine_owners.items() if o == owner_id]

    def snapshot_bindings(self) -> List[Tuple[str, str]]:
        """Return a copy of the current ``(machine_id, owner_id)`` bindings."""
        with self._lock:
            return list(self._machine_owners.items())
