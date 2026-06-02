"""Break-glass bootstrap: the one-time admin escape hatch.

Break-glass is the *single, provider-independent* way into the control plane.
It exists to solve exactly two orthogonal problems and nothing else:

1. **Bootstrap** — the very first admin has no account yet, so they need a way
   in to create accounts / configure an auth provider.
2. **Fail-closed fallback** — when the configured auth provider is unreachable
   or misconfigured (and the request boundary therefore refuses everyone), an
   operator still needs an emergency entrance.

It is deliberately a *single admin subject*, not a multi-owner mechanism:
distinguishing trust domains is the job of the built-in
:class:`~se3.server.auth.local.LocalAuthProvider` (or an enabled OIDC). A
break-glass token MUST NOT be used as "one token per user".

Security properties (mirrored from the multi-tenant server design):

- The token plaintext is generated with :func:`se3.server.crypto.generate_token`
  (256 bits of entropy) and printed to the server console **exactly once** by
  the issuing CLI; only its SHA-256 hash is persisted in ``breakglass_tokens``.
- Re-issuable: minting a fresh token purges any prior outstanding tokens, so an
  operator can always rotate the escape hatch ("re-sign overwrites old").
- One-time / temporary: a token is consumed atomically (valid at most once) and
  may additionally carry an absolute expiry; a consumed or expired token fails.
- Validation is constant-time: the presented plaintext is hashed and matched
  against the stored hash through the persistence layer's keyed lookup (the
  stored value is a SHA-256 hash, so no plaintext comparison ever occurs), and
  the crypto layer's comparisons go through :func:`hmac.compare_digest`.
- The token is **never** written to any log — neither on issue nor on consume.
  Only non-secret facts (a token *id*, an outcome) may be logged.

Importing this module is safe on a core-only install: it pulls in only the
stdlib-``sqlite3`` persistence layer and the crypto helpers (whose argon2/bcrypt
backends are deferred). The FastAPI / session machinery is imported lazily and
only on the server-side consume path, so ``se3-server bootstrap-token`` can mint
a token without the ``se3[server]`` extra installed — which is precisely what
makes break-glass usable as a fail-closed escape hatch.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional, Tuple

from . import crypto
from .persistence import Store

logger = logging.getLogger(__name__)

#: Token prefix so the plaintext is recognizable as a break-glass token (and a
#: stray copy is at least distinguishable from a daemon key / session id).
BREAKGLASS_TOKEN_PREFIX = "se3bg"

#: The stable internal ``owner_id`` of the single break-glass admin subject.
#: Break-glass always resolves to this one owner; multi-owner separation is the
#: local/OIDC provider's job, never break-glass's.
BREAKGLASS_ADMIN_OWNER_ID = "breakglass-admin"
BREAKGLASS_ADMIN_DISPLAY_NAME = "break-glass admin"

#: ``provider`` discriminator stamped on a break-glass :class:`OwnerIdentity`
#: (diagnostics only — never used for authorization).
BREAKGLASS_PROVIDER = "breakglass"


# --------------------------------------------------------------------------- #
# Issue / consume                                                             #
# --------------------------------------------------------------------------- #


def issue_breakglass_token(
    store: Store,
    *,
    ttl_seconds: Optional[float] = None,
    reissue: bool = True,
) -> str:
    """Mint a break-glass token and return its **plaintext** (once).

    The plaintext is the secret to print to the server console exactly once; it
    is never stored — only its hash reaches the ``breakglass_tokens`` table.

    Args:
        store: the persistence :class:`Store`.
        ttl_seconds: optional lifetime; the token also expires this many seconds
            from now (in addition to being one-time-consumable). ``None`` ⇒ no
            time-based expiry (rotation / consumption are the only invalidators).
        reissue: when ``True`` (default), purge any outstanding break-glass
            tokens first so re-signing overwrites the old escape hatch.

    Returns:
        The token plaintext. Callers MUST NOT log it; print it to the console
        once and discard it.
    """
    if reissue:
        purged = store.purge_breakglass()
        if purged:
            logger.info("break-glass: purged %d outstanding token(s) before re-issue", purged)

    plaintext, token_hash = crypto.generate_token(BREAKGLASS_TOKEN_PREFIX)
    expires_at = (time.time() + ttl_seconds) if ttl_seconds is not None else None
    token_id = store.put_breakglass(token_hash, expires_at=expires_at)
    # Log the non-secret id only; the plaintext never touches the log.
    logger.info("break-glass: issued admin token id=%s (expires_at=%s)", token_id, expires_at)
    return plaintext


def consume_breakglass_token(store: Store, token: Optional[str]) -> bool:
    """Atomically consume a break-glass token. Returns ``True`` at most once.

    Returns ``True`` for a valid, unexpired, not-yet-consumed token (marking it
    consumed in the same transaction), ``False`` for an empty / unknown /
    already-consumed / expired token. The check-and-mark is serialized inside
    the store, so a token can never be consumed twice.

    Validation is constant-time in the secret: the plaintext is hashed and the
    store matches by that hash, never by the plaintext itself.
    """
    if not token:
        return False
    ok = store.consume_breakglass(crypto.token_hash(token))
    # Never log the token; only the binary outcome.
    logger.info("break-glass: token consume %s", "accepted" if ok else "rejected")
    return ok


# --------------------------------------------------------------------------- #
# Break-glass admin owner + full entry (server side)                          #
# --------------------------------------------------------------------------- #


def ensure_breakglass_admin(store: Store) -> str:
    """Ensure the single break-glass admin owner exists; return its owner_id.

    Idempotent: the admin owner is created once with a stable internal
    ``owner_id`` and the admin flag set, so every break-glass entry resolves to
    the same single admin subject.
    """
    owner = store.get_owner(BREAKGLASS_ADMIN_OWNER_ID)
    if owner is None:
        store.create_owner(
            BREAKGLASS_ADMIN_DISPLAY_NAME,
            is_admin=True,
            owner_id=BREAKGLASS_ADMIN_OWNER_ID,
        )
        logger.info("break-glass: created admin owner %s", BREAKGLASS_ADMIN_OWNER_ID)
    return BREAKGLASS_ADMIN_OWNER_ID


def consume_breakglass_login(
    store: Store, sessions: Any, token: Optional[str]
) -> Optional[Tuple[str, Any]]:
    """Consume a break-glass token and establish an admin session.

    This is the **provider-independent** entry used by the
    ``POST /api/auth/breakglass`` endpoint: it never consults the auth provider
    chain, so it still works when the configured provider is unreachable or no
    provider is configured at all (the fail-closed escape hatch).

    On a valid token it consumes it (one-time), ensures the single break-glass
    admin owner, mints a server-side session for that owner, and returns
    ``(session_id_plaintext, OwnerIdentity)`` — the caller sets the session
    cookie from ``session_id_plaintext``. Returns ``None`` for an
    invalid / expired / already-consumed token.

    ``sessions`` is a :class:`~se3.server.auth.session.SessionStore`; it is
    accepted as ``Any`` so this module need not import the auth package at load
    time (keeping the core-only CLI path light).
    """
    if not consume_breakglass_token(store, token):
        return None
    # Deferred: keep the auth package off the core-only `bootstrap-token` path.
    from .auth.base import OwnerIdentity

    owner_id = ensure_breakglass_admin(store)
    session_id, _session = sessions.create(owner_id)
    owner = store.get_owner(owner_id)
    identity = OwnerIdentity(
        owner_id=owner_id,
        display_name=owner.display_name if owner else BREAKGLASS_ADMIN_DISPLAY_NAME,
        provider=BREAKGLASS_PROVIDER,
        is_admin=True,
    )
    logger.info("break-glass: admin session established for owner %s", owner_id)
    return session_id, identity


# --------------------------------------------------------------------------- #
# CLI: `se3-server bootstrap-token`                                            #
# --------------------------------------------------------------------------- #


def format_announcement(token: str, *, ttl_seconds: Optional[float] = None) -> str:
    """Build the one-time console announcement carrying the token plaintext.

    This is the *only* place the plaintext is surfaced. It is returned (not
    logged) so the CLI can ``print`` it straight to stdout. Callers MUST NOT
    route this string through the logging system.
    """
    lines = [
        "",
        "=" * 70,
        "SE3 break-glass admin token (shown ONCE — copy it now):",
        "",
        f"    {token}",
        "",
        "Use it at POST /api/auth/breakglass to enter as the single break-glass",
        "admin, then create accounts / configure an auth provider. It is a",
        "one-time token: it stops working the moment it is used.",
    ]
    if ttl_seconds is not None:
        lines.append(f"It also expires {int(ttl_seconds)}s from now.")
    lines.append("Re-run `se3-server bootstrap-token` to rotate / re-issue it.")
    lines.append("=" * 70)
    lines.append("")
    return "\n".join(lines)


def run_bootstrap_token_cli(argv: Optional[list] = None) -> int:
    """``se3-server bootstrap-token`` — mint and print a break-glass token.

    Deliberately depends only on the stdlib-``sqlite3`` persistence layer and
    the crypto helpers, so it runs on a core-only install (no ``se3[server]``
    extra) — that is what keeps break-glass available as a fail-closed escape
    hatch even when the web stack / auth provider is broken. The token plaintext
    is printed to stdout exactly once and never logged.

    Returns a process exit code (0 on success).
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="se3-server bootstrap-token",
        description=(
            "Issue a one-time break-glass admin token (printed once, stored "
            "hashed). Re-issuing rotates and invalidates any prior token."
        ),
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="SQLite DB path (default: server config's db_path, ~/.se3/server.db)",
    )
    parser.add_argument(
        "--ttl-minutes",
        type=float,
        default=None,
        help="Optional expiry in minutes (default: no time-based expiry)",
    )
    parser.add_argument(
        "--keep-existing",
        action="store_true",
        help="Do not purge prior outstanding tokens (default: re-issue / overwrite)",
    )
    args = parser.parse_args(argv)

    db_path = args.db_path
    if not db_path:
        # Resolve from the central-server config (global + project YAML).
        from se3.config import load_server_config

        db_path = str(load_server_config().resolved_db_path())

    store = Store(db_path)
    try:
        ttl_seconds = args.ttl_minutes * 60.0 if args.ttl_minutes is not None else None
        token = issue_breakglass_token(
            store, ttl_seconds=ttl_seconds, reissue=not args.keep_existing
        )
        # Print straight to stdout — the single, un-logged disclosure point.
        print(format_announcement(token, ttl_seconds=ttl_seconds))
    finally:
        store.close()
    return 0
