"""Tests for se3.server.persistence (sqlite repository) and
se3.server.identity (owner/key resolution + machine binding) — G2.

Covers: five-table CRUD, owner_id decoupling from (provider, external_id),
binding resolution, daemon-key issue/resolve/revoke, one-time break-glass
consumption + re-issue, DB file path + WAL, and the identity service's
key→owner resolution, machine binding, and cross-provider link trust gate.
"""

from __future__ import annotations

import threading

import pytest

from se3.server import crypto
from se3.server.identity import IdentityService, UntrustedIdentityLink
from se3.server.persistence import IdentityAlreadyBound, Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "server.db")


# --------------------------------------------------------------------------- #
# Owners + identity bindings                                                  #
# --------------------------------------------------------------------------- #


def test_owner_id_is_stable_internal_key(store):
    oid = store.create_owner("Alice", is_admin=True)
    assert isinstance(oid, str) and oid
    owner = store.get_owner(oid)
    assert owner is not None
    assert owner.owner_id == oid
    assert owner.display_name == "Alice"
    assert owner.is_admin is True


def test_get_unknown_owner_returns_none(store):
    assert store.get_owner("does-not-exist") is None


def test_owner_decoupled_from_provider_identity_with_many_bindings(store):
    oid = store.create_owner("Bob")
    # A single owner carries multiple (provider, external_id) bindings.
    store.link_identity(oid, "local", "bob")
    store.link_identity(oid, "oidc", "https://idp.example|sub-123")
    store.link_identity(oid, "proxy_header", "bob@example.com")

    assert store.resolve_owner_by_identity("local", "bob") == oid
    assert store.resolve_owner_by_identity("oidc", "https://idp.example|sub-123") == oid
    assert store.resolve_owner_by_identity("proxy_header", "bob@example.com") == oid
    # owner_id itself is unrelated to any of these external identifiers.
    assert oid not in ("bob", "bob@example.com")
    assert sorted(store.list_identities(oid)) == sorted(
        [
            ("local", "bob"),
            ("oidc", "https://idp.example|sub-123"),
            ("proxy_header", "bob@example.com"),
        ]
    )


def test_create_local_user_atomic_owner_binding_password(store):
    pw_hash = crypto.hash_password("s3cret")
    oid = store.create_local_user(
        "local", "bob", pw_hash, display_name="Bob", is_admin=True
    )
    # All three facts landed in one transaction.
    assert store.resolve_owner_by_identity("local", "bob") == oid
    owner = store.get_owner(oid)
    assert owner.display_name == "Bob" and owner.is_admin is True
    assert store.get_password_hash(oid) == pw_hash


def test_create_local_user_duplicate_raises_and_leaves_no_orphan(store):
    pw_hash = crypto.hash_password("pw")
    first = store.create_local_user("local", "dup", pw_hash)
    before = {o.owner_id for o in store.list_owners()}
    with pytest.raises(IdentityAlreadyBound):
        store.create_local_user("local", "dup", pw_hash)
    # The rolled-back attempt created no owner row.
    after = {o.owner_id for o in store.list_owners()}
    assert after == before
    assert store.resolve_owner_by_identity("local", "dup") == first


def test_resolve_unknown_identity_returns_none(store):
    assert store.resolve_owner_by_identity("local", "nobody") is None


def test_link_identity_idempotent_same_owner(store):
    oid = store.create_owner()
    store.link_identity(oid, "local", "carol")
    store.link_identity(oid, "local", "carol")  # no error, no duplicate
    assert store.list_identities(oid) == [("local", "carol")]


def test_link_identity_conflict_different_owner_raises(store):
    a = store.create_owner("A")
    b = store.create_owner("B")
    store.link_identity(a, "local", "shared")
    with pytest.raises(IdentityAlreadyBound):
        store.link_identity(b, "local", "shared")
    # still bound to the original owner
    assert store.resolve_owner_by_identity("local", "shared") == a


# --------------------------------------------------------------------------- #
# Local credentials                                                           #
# --------------------------------------------------------------------------- #


def test_password_hash_roundtrip_and_upsert(store):
    oid = store.create_owner("Dave")
    assert store.get_password_hash(oid) is None
    h1 = crypto.hash_password("first-pw")
    store.set_password(oid, h1)
    assert store.get_password_hash(oid) == h1
    # upsert replaces
    h2 = crypto.hash_password("second-pw")
    store.set_password(oid, h2)
    assert store.get_password_hash(oid) == h2


def test_password_stored_is_hash_not_plaintext(store):
    oid = store.create_owner()
    store.set_password(oid, crypto.hash_password("plaintext-secret"))
    stored = store.get_password_hash(oid)
    assert "plaintext-secret" not in stored
    assert crypto.verify_password("plaintext-secret", stored) is True


# --------------------------------------------------------------------------- #
# Daemon keys                                                                 #
# --------------------------------------------------------------------------- #


def test_issue_and_resolve_daemon_key_hash_only(store):
    oid = store.create_owner("Eve")
    plaintext, key_hash = crypto.generate_token("sek")
    key_id = store.issue_daemon_key(oid, key_hash, label="laptop")
    assert key_id
    # Only the hash is stored; the resolver matches by hash of the presented key.
    assert store.resolve_owner_by_daemon_key(key_hash) == oid
    keys = store.list_daemon_keys(oid)
    assert len(keys) == 1
    assert keys[0].key_hash == key_hash
    assert plaintext not in keys[0].key_hash


def test_revoke_daemon_key_then_resolve_fails(store):
    oid = store.create_owner()
    _plain, key_hash = crypto.generate_token("sek")
    key_id = store.issue_daemon_key(oid, key_hash)
    assert store.resolve_owner_by_daemon_key(key_hash) == oid
    assert store.revoke_daemon_key(key_id) is True
    assert store.resolve_owner_by_daemon_key(key_hash) is None
    # listed as revoked but no longer resolvable
    keys = store.list_daemon_keys(oid)
    assert keys[0].revoked is True
    assert store.list_daemon_keys(oid, include_revoked=False) == []


def test_revoke_unknown_daemon_key_returns_false(store):
    assert store.revoke_daemon_key("nope") is False


def test_revoke_is_idempotent(store):
    oid = store.create_owner()
    _plain, key_hash = crypto.generate_token("sek")
    key_id = store.issue_daemon_key(oid, key_hash)
    assert store.revoke_daemon_key(key_id) is True
    assert store.revoke_daemon_key(key_id) is True  # still success, no-op


# --------------------------------------------------------------------------- #
# Break-glass tokens                                                          #
# --------------------------------------------------------------------------- #


def test_breakglass_one_time_consume(store):
    plaintext, token_hash = crypto.generate_token("sebg")
    store.put_breakglass(token_hash)
    assert store.consume_breakglass(token_hash) is True
    # second consume fails (one-time)
    assert store.consume_breakglass(token_hash) is False


def test_breakglass_unknown_token_fails(store):
    assert store.consume_breakglass("0" * 64) is False


def test_breakglass_expiry(store):
    _plain, token_hash = crypto.generate_token("sebg")
    store.put_breakglass(token_hash, expires_at=1000.0)
    assert store.consume_breakglass(token_hash, now=2000.0) is False  # expired
    # an unexpired one consumes fine
    _p2, th2 = crypto.generate_token("sebg")
    store.put_breakglass(th2, expires_at=5000.0)
    assert store.consume_breakglass(th2, now=1000.0) is True


def test_breakglass_reissue_after_purge(store):
    _p1, th1 = crypto.generate_token("sebg")
    store.put_breakglass(th1)
    assert store.purge_breakglass() == 1
    assert store.consume_breakglass(th1) is False  # old token invalidated
    # re-issue a fresh token
    _p2, th2 = crypto.generate_token("sebg")
    store.put_breakglass(th2)
    assert store.consume_breakglass(th2) is True


def test_breakglass_only_hash_stored(store):
    plaintext, token_hash = crypto.generate_token("sebg")
    store.put_breakglass(token_hash)
    # The resolver works only via hash of the presented token; the plaintext
    # is never persisted (we can only match by recomputed hash).
    assert store.consume_breakglass(crypto.token_hash(plaintext)) is True


# --------------------------------------------------------------------------- #
# Persistence properties: file path + cross-connection durability + WAL       #
# --------------------------------------------------------------------------- #


def test_db_path_from_config_and_persists_across_instances(tmp_path):
    db = tmp_path / "nested" / "server.db"
    s1 = Store(db)
    oid = s1.create_owner("Persisted")
    _plain, kh = crypto.generate_token("sek")
    s1.issue_daemon_key(oid, kh)
    s1.close()
    assert db.exists()  # parent dirs auto-created, single file

    # A fresh Store on the same file sees the data (durable source of truth).
    s2 = Store(db)
    assert s2.get_owner(oid) is not None
    assert s2.resolve_owner_by_daemon_key(kh) == oid


def test_wal_mode_enabled(tmp_path):
    db = tmp_path / "server.db"
    store = Store(db)
    mode = store._conn().execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_concurrent_writes_are_safe(tmp_path):
    store = Store(tmp_path / "server.db")
    errors = []

    def worker(n):
        try:
            for i in range(20):
                store.create_owner(f"owner-{n}-{i}")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(store.list_owners()) == 4 * 20


# --------------------------------------------------------------------------- #
# IdentityService (identity.py)                                               #
# --------------------------------------------------------------------------- #


def test_identity_resolve_owner_for_key_delegates_to_store(store):
    oid = store.create_owner("Frank")
    plaintext, key_hash = crypto.generate_token("sek")
    store.issue_daemon_key(oid, key_hash)
    svc = IdentityService(store)
    assert svc.resolve_owner_for_key(plaintext) == oid
    # missing / unknown key → None
    assert svc.resolve_owner_for_key("") is None
    assert svc.resolve_owner_for_key(None) is None
    assert svc.resolve_owner_for_key("sek_bogus") is None


def test_identity_revoked_key_no_longer_resolves(store):
    oid = store.create_owner()
    plaintext, key_hash = crypto.generate_token("sek")
    key_id = store.issue_daemon_key(oid, key_hash)
    svc = IdentityService(store)
    assert svc.resolve_owner_for_key(plaintext) == oid
    store.revoke_daemon_key(key_id)
    assert svc.resolve_owner_for_key(plaintext) is None


def test_identity_machine_binding(store):
    svc = IdentityService(store)
    assert svc.owner_of_machine("m1") is None
    svc.bind_machine("m1", "owner-a")
    svc.bind_machine("m2", "owner-a")
    svc.bind_machine("m3", "owner-b")
    assert svc.owner_of_machine("m1") == "owner-a"
    assert sorted(svc.machines_for_owner("owner-a")) == ["m1", "m2"]
    svc.unbind_machine("m1")
    assert svc.owner_of_machine("m1") is None


def test_identity_link_seam_refuses_unverified_blind_merge(store):
    a = store.create_owner("A")
    svc = IdentityService(store)
    # Default: no trust gate → refuses (account-takeover guard).
    with pytest.raises(UntrustedIdentityLink):
        svc.link_identity_to_owner(a, "oidc", "issuer|sub-9")
    assert store.resolve_owner_by_identity("oidc", "issuer|sub-9") is None

    # Admin override passes the gate.
    svc.link_identity_to_owner(a, "oidc", "issuer|sub-9", admin_override=True)
    assert store.resolve_owner_by_identity("oidc", "issuer|sub-9") == a

    # Verified claim also passes the gate (different identity).
    svc.link_identity_to_owner(a, "proxy_header", "a@example.com", verified_claim=True)
    assert store.resolve_owner_by_identity("proxy_header", "a@example.com") == a


def test_identity_link_idempotent_and_conflict(store):
    a = store.create_owner("A")
    b = store.create_owner("B")
    svc = IdentityService(store)
    svc.link_identity_to_owner(a, "local", "alice", admin_override=True)
    # Re-link to the same owner is idempotent and bypasses the gate.
    assert svc.link_identity_to_owner(a, "local", "alice") == a
    # Linking to a different owner conflicts.
    with pytest.raises(IdentityAlreadyBound):
        svc.link_identity_to_owner(b, "local", "alice", admin_override=True)
