"""Tests for se3.server.crypto — slow password hashing, token generation,
constant-time comparison, and redaction helpers (G2)."""

from __future__ import annotations

from se3.server import crypto


# --------------------------------------------------------------------------- #
# Password hashing                                                            #
# --------------------------------------------------------------------------- #


def test_hash_password_is_slow_salted_not_plaintext():
    pw = "correct horse battery staple"
    h = crypto.hash_password(pw)
    # Never plaintext; always a recognized slow-hash format (argon2 / bcrypt).
    assert h != pw
    assert pw not in h
    assert h.startswith("$argon2") or h.startswith(("$2a$", "$2b$", "$2y$"))


def test_hash_password_salted_unique_per_call():
    pw = "hunter2"
    assert crypto.hash_password(pw) != crypto.hash_password(pw)


def test_verify_password_roundtrip():
    pw = "S3cr3t-Pa$$w0rd"
    h = crypto.hash_password(pw)
    assert crypto.verify_password(pw, h) is True
    assert crypto.verify_password("wrong", h) is False


def test_verify_password_handles_long_and_unicode():
    pw = "πψω" * 100  # > 72 bytes, multibyte
    h = crypto.hash_password(pw)
    assert crypto.verify_password(pw, h) is True
    assert crypto.verify_password(pw + "x", h) is False


def test_verify_password_rejects_malformed_hash():
    assert crypto.verify_password("anything", "") is False
    assert crypto.verify_password("anything", "not-a-hash") is False
    assert crypto.verify_password("anything", "$unknown$xx") is False


# --------------------------------------------------------------------------- #
# Token generation + hashed storage                                           #
# --------------------------------------------------------------------------- #


def test_generate_token_high_entropy_and_unique():
    p1, h1 = crypto.generate_token("sek")
    p2, h2 = crypto.generate_token("sek")
    assert p1 != p2
    assert h1 != h2
    assert p1.startswith("sek_")
    # url-safe base64 of 32 bytes is ~43 chars => plenty of entropy.
    assert len(p1) > 40


def test_generate_token_no_prefix():
    plaintext, h = crypto.generate_token()
    assert plaintext  # non-empty, no leading prefix underscore
    assert not plaintext.startswith("_")
    assert h == crypto.token_hash(plaintext)


def test_token_hash_matches_and_verifies():
    plaintext, h = crypto.generate_token("bg")
    assert crypto.token_hash(plaintext) == h
    assert crypto.verify_token_hash(plaintext, h) is True
    assert crypto.verify_token_hash(plaintext + "x", h) is False
    assert crypto.verify_token_hash("", h) is False


def test_token_plaintext_not_derivable_from_hash():
    plaintext, h = crypto.generate_token("sek")
    # Storage hash must not contain the secret.
    assert plaintext not in h
    assert len(h) == 64  # sha256 hex


# --------------------------------------------------------------------------- #
# Constant-time comparison + redaction                                        #
# --------------------------------------------------------------------------- #


def test_const_eq_str_and_bytes():
    assert crypto.const_eq("abc", "abc") is True
    assert crypto.const_eq(b"abc", b"abc") is True
    assert crypto.const_eq("abc", b"abc") is True
    assert crypto.const_eq("abc", "abd") is False
    assert crypto.const_eq("abc", "abcd") is False


def test_const_eq_bad_types_return_false():
    assert crypto.const_eq("abc", None) is False
    assert crypto.const_eq(123, "abc") is False


def test_redact_reveals_nothing():
    secret = "sek_supersecretvalue"
    red = crypto.redact(secret)
    assert secret not in red
    assert red == "<redacted>"
    assert crypto.redact("") == "<empty>"
    assert crypto.redact(None) == "<empty>"


def test_token_fingerprint_is_short_and_non_reversible():
    plaintext, _ = crypto.generate_token("sek")
    fp = crypto.token_fingerprint(plaintext)
    assert plaintext not in fp
    assert len(fp) == 12
    # Deterministic and tied to the value.
    assert fp == crypto.token_fingerprint(plaintext)
    assert crypto.token_fingerprint("") == "<empty>"
