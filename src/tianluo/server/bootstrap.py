"""Break-glass bootstrap — minting the one-time admin escape-hatch token.

The break-glass token is the single, IdP-independent escape hatch into the
multi-tenant control plane (see the design's break-glass section). It is *not*
a daily login and never distinguishes trust domains — it is one admin subject
used only for two orthogonal problems:

1. **bootstrap** — the first admin uses it to get in and create accounts /
   configure an auth provider;
2. **fail-closed fallback** — an emergency entrance when the configured auth
   provider is unreachable.

Distinguishing real owners is the local auth provider's job; break-glass never
models multiple trust domains.

This module is deliberately dependency-light: it pulls in only the persistence
layer (stdlib ``sqlite3``) and the crypto helpers, never FastAPI / uvicorn.
That keeps ``tianluo-server bootstrap-token`` off the heavy web import chain so it
works even on a core-only install, and is why ``tianluo.server.__init__.main``
intercepts the subcommand *before* importing the ``[server]`` extra.

Security invariants:

* The token plaintext is generated, only its SHA-256 *hash* is persisted (via
  :meth:`Store.put_breakglass`), and the plaintext is printed to the server
  console exactly once — never stored, never logged.
* Issuance is re-runnable (a fresh token each time); previously minted tokens
  stay valid until consumed or purged.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional, TextIO, Tuple

#: Default on-disk location of the server's sqlite store. The running server
#: (``tianluo-server``) defaults to the same path, so a token minted here is
#: consumable by that server's ``POST /api/auth/breakglass``.
DEFAULT_DB_PATH = "~/.se3/server.db"


def issue_breakglass_token(
    store, *, ttl_seconds: Optional[float] = None
) -> Tuple[str, str]:
    """Mint a break-glass token against *store*; return ``(plaintext, token_id)``.

    Only the token *hash* is persisted; the returned plaintext is the secret to
    print **once** and is never stored or logged by this function.
    """
    from . import crypto

    plaintext, token_hash = crypto.generate_token("bg")
    expires_at: Optional[float] = None
    if ttl_seconds is not None:
        import time

        expires_at = time.time() + float(ttl_seconds)
    token_id = store.put_breakglass(token_hash, expires_at=expires_at)
    return plaintext, token_id


def print_breakglass_token(
    db_path: str = DEFAULT_DB_PATH,
    *,
    ttl_seconds: Optional[float] = None,
    stream: Optional[TextIO] = None,
) -> str:
    """Open the store at *db_path*, mint a token, print it once, return the plaintext.

    The plaintext is written to *stream* (default ``sys.stdout``) — the single
    console reveal the design mandates — and returned for programmatic callers
    (tests). It is never returned through any persisted record.
    """
    import sys

    from .persistence import Store

    out = stream if stream is not None else sys.stdout
    store = Store(Path(db_path).expanduser())
    plaintext, token_id = issue_breakglass_token(store, ttl_seconds=ttl_seconds)
    _print_banner(out, plaintext, token_id, ttl_seconds)
    return plaintext


def _print_banner(
    out: TextIO, plaintext: str, token_id: str, ttl_seconds: Optional[float]
) -> None:
    rule = "=" * 64
    lines = [
        "",
        rule,
        "  tianluo-server break-glass admin token (shown ONCE — copy it now)",
        rule,
        f"  token:  {plaintext}",
        f"  id:     {token_id}",
    ]
    if ttl_seconds is not None:
        lines.append(f"  expires in: {int(ttl_seconds)}s")
    lines += [
        "",
        "  Present it at POST /api/auth/breakglass (or the web login's",
        "  break-glass field) to mint a one-time admin session. It is",
        "  single-use; re-run this command to mint another.",
        rule,
        "",
    ]
    out.write("\n".join(lines) + "\n")


def main(argv: Optional[list] = None) -> int:
    """``tianluo-server bootstrap-token`` subcommand entry point.

    Dependency-light by design: it never imports FastAPI / uvicorn, so it works
    on a core-only install as well as on the server host.
    """
    parser = argparse.ArgumentParser(
        prog="tianluo-server bootstrap-token",
        description="Mint a one-time break-glass admin token (printed once).",
    )
    parser.add_argument(
        "--db-path",
        default=DEFAULT_DB_PATH,
        help=f"Path to the server sqlite store (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--ttl",
        type=float,
        default=None,
        help="Optional time-to-live in seconds (default: no expiry)",
    )
    args = parser.parse_args(argv)
    print_breakglass_token(args.db_path, ttl_seconds=args.ttl)
    return 0
