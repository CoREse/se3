"""Credential cryptography for the se3 multi-tenant server.

Slow password hashing (argon2id preferred, bcrypt fallback), high-entropy
token generation + hashed storage (daemon keys / break-glass tokens /
session ids), and constant-time comparison helpers.

Security baseline (see the multi-tenant server design):

- Passwords are stored with a slow, salted hash (argon2id / bcrypt) — never
  plaintext, never a fast unsalted hash.
- Random tokens (daemon key, break-glass token, session id) are high-entropy
  and need no slow hash; only their SHA-256 hash is persisted, and the
  plaintext is returned to the caller exactly once.
- All credential comparisons go through :func:`hmac.compare_digest` to avoid
  timing oracles.
- Credentials must never be logged; :func:`redact` / :func:`token_fingerprint`
  produce safe, non-reversible display forms.

Heavy hashing backends (``argon2-cffi`` / ``bcrypt``) ship only in the
``se3[server]`` optional-dependency extra. Their imports are deferred into the
functions that need them, so merely importing this module — or any
``se3.server`` submodule that transitively pulls it in — never raises on a
core-only install; the missing-backend error only surfaces when password
hashing is actually attempted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Optional, Tuple

# Number of random bytes behind a generated token. 32 bytes ≈ 256 bits of
# entropy, rendered to ~43 url-safe base64 chars, which makes the hashed
# tokens (daemon keys / break-glass) infeasible to brute-force — hence a fast
# SHA-256 hash for *storage* of these tokens is sufficient (unlike passwords).
_TOKEN_NBYTES = 32

# Recognized stored-hash prefixes for password verification dispatch.
_ARGON2_PREFIX = "$argon2"
_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")


class PasswordBackendUnavailable(RuntimeError):
    """Raised when neither argon2-cffi nor bcrypt is importable.

    Indicates the ``se3[server]`` extra (which ships ``argon2-cffi``) is not
    installed. The core CLI never triggers this; it only fires on the server
    auth path when a password hash/verify is actually attempted.
    """


# --------------------------------------------------------------------------- #
# Password hashing (slow, salted)                                             #
# --------------------------------------------------------------------------- #


def _argon2_hasher():
    """Return an argon2 ``PasswordHasher`` or ``None`` if unavailable."""
    try:
        from argon2 import PasswordHasher
    except Exception:  # pragma: no cover - import-environment dependent
        return None
    return PasswordHasher()


def _bcrypt_module():
    """Return the ``bcrypt`` module or ``None`` if unavailable."""
    try:
        import bcrypt
    except Exception:  # pragma: no cover - import-environment dependent
        return None
    return bcrypt


def _bcrypt_prep(password: str) -> bytes:
    """Normalize a password for bcrypt's 72-byte / NUL-byte limitations.

    bcrypt silently truncates input past 72 bytes and rejects embedded NULs.
    Pre-hashing with SHA-256 and base64-encoding produces a fixed-length,
    NUL-free input so arbitrarily long / unicode passwords are fully covered.
    The same transform is applied on both hash and verify (the stored hash's
    ``$2`` prefix tells us to apply it), so it round-trips correctly.
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    """Hash ``password`` with a slow, salted algorithm.

    Prefers argon2id (``argon2-cffi``); falls back to bcrypt when argon2 is not
    installed. The returned string embeds the algorithm, parameters, and salt
    (PHC / bcrypt format) so :func:`verify_password` is self-describing.

    Raises :class:`PasswordBackendUnavailable` when no backend is importable.
    """
    if not isinstance(password, str):
        raise TypeError("password must be a str")

    hasher = _argon2_hasher()
    if hasher is not None:
        return hasher.hash(password)

    bcrypt = _bcrypt_module()
    if bcrypt is not None:
        return bcrypt.hashpw(_bcrypt_prep(password), bcrypt.gensalt()).decode("ascii")

    raise PasswordBackendUnavailable(
        "no password-hashing backend available; install the se3[server] "
        "extra (argon2-cffi) — passwords must never be stored without a slow hash"
    )


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time-ish verification of ``password`` against ``stored_hash``.

    Dispatches on the stored hash's algorithm prefix. Both argon2's
    ``verify`` and bcrypt's ``checkpw`` perform their comparison in constant
    time internally. Returns ``False`` (never raises) on mismatch, malformed
    hash, or unknown format.
    """
    if not isinstance(password, str) or not isinstance(stored_hash, str) or not stored_hash:
        return False

    if stored_hash.startswith(_ARGON2_PREFIX):
        hasher = _argon2_hasher()
        if hasher is None:
            return False
        try:
            return bool(hasher.verify(stored_hash, password))
        except Exception:
            return False

    if stored_hash.startswith(_BCRYPT_PREFIXES):
        bcrypt = _bcrypt_module()
        if bcrypt is None:
            return False
        try:
            return bool(bcrypt.checkpw(_bcrypt_prep(password), stored_hash.encode("ascii")))
        except Exception:
            return False

    return False


# --------------------------------------------------------------------------- #
# Token generation + hashed storage (daemon keys / break-glass / sessions)    #
# --------------------------------------------------------------------------- #


def token_hash(plaintext: str) -> str:
    """Return the hex SHA-256 of a token plaintext, for hashed storage.

    Tokens are high-entropy random strings (see :func:`generate_token`), so a
    fast deterministic hash is appropriate here — it lets the server look a
    token up by hashing the value it receives (e.g. a daemon key in HELLO) and
    matching against the stored hash, while the plaintext itself is never
    persisted.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_token(prefix: str = "", *, nbytes: int = _TOKEN_NBYTES) -> Tuple[str, str]:
    """Generate a high-entropy token and its storage hash.

    Returns ``(plaintext, hash)``:

    - ``plaintext`` is ``f"{prefix}_{random}"`` (or just ``random`` when no
      prefix is given), where ``random`` is ``secrets.token_urlsafe(nbytes)``.
      This is the secret to hand to the user **once** and never store.
    - ``hash`` is :func:`token_hash` of the plaintext — the only thing to
      persist.
    """
    random_part = secrets.token_urlsafe(nbytes)
    plaintext = f"{prefix}_{random_part}" if prefix else random_part
    return plaintext, token_hash(plaintext)


def verify_token_hash(plaintext: str, stored_hash: str) -> bool:
    """Constant-time check that ``plaintext`` hashes to ``stored_hash``."""
    if not isinstance(plaintext, str) or not isinstance(stored_hash, str) or not stored_hash:
        return False
    return const_eq(token_hash(plaintext), stored_hash)


# --------------------------------------------------------------------------- #
# Comparison + redaction helpers                                              #
# --------------------------------------------------------------------------- #


def const_eq(a, b) -> bool:
    """Constant-time equality for two ``str`` or ``bytes`` values.

    Wraps :func:`hmac.compare_digest`. ``str`` inputs are UTF-8 encoded so a
    caller may pass either type; mismatched / unsupported types return
    ``False`` rather than raising.
    """
    if isinstance(a, str):
        a = a.encode("utf-8")
    if isinstance(b, str):
        b = b.encode("utf-8")
    if not isinstance(a, (bytes, bytearray)) or not isinstance(b, (bytes, bytearray)):
        return False
    return hmac.compare_digest(bytes(a), bytes(b))


def token_fingerprint(plaintext: str, *, length: int = 12) -> str:
    """Return a short, non-reversible fingerprint of a secret for logs/UI.

    Useful to *identify* which token is meant (e.g. in an audit line) without
    exposing the secret or its length. Derived from the SHA-256 hash, so it
    leaks nothing about the plaintext.
    """
    if not plaintext:
        return "<empty>"
    return token_hash(plaintext)[:length]


def redact(secret: Optional[str]) -> str:
    """Return a safe placeholder for a credential, revealing nothing.

    Use anywhere a credential might otherwise be interpolated into a log line
    or message. Deliberately exposes neither the value nor its length.
    """
    if not secret:
        return "<empty>"
    return "<redacted>"
