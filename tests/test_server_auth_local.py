"""Tests for the pluggable auth layer (G3): AuthProvider/ProviderChain, the
session store + cookie resolver, the default LocalAuthProvider (password +
session, multi-owner, no external IdP), login rate limiting / lockout, the
registry fail-closed assembly + require_owner dependency, and the
OIDC / proxy-header optional-provider seams (disabled by default)."""

from __future__ import annotations

import logging

import pytest

from se3.server import crypto
from se3.server.auth import (
    AuthNotConfigured,
    LocalAuthProvider,
    LoginRateLimited,
    LoginRateLimiter,
    OwnerIdentity,
    ProviderChain,
    RateLimitConfig,
    SessionStore,
    build_provider_chain,
    make_require_owner,
)
from se3.server.auth.base import AuthProvider
from se3.server.auth.oidc import OidcProvider, oidc_external_id
from se3.server.auth.proxy_header import ProxyHeaderProvider
from se3.server.auth.session import CookieConfig, SessionAuthProvider
from se3.server.persistence import Store


# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                          #
# --------------------------------------------------------------------------- #


class _Clock:
    """A controllable monotonic-ish clock for deterministic expiry/lockout."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeRequest:
    """Minimal request-like object exposing ``.cookies`` / ``.headers``."""

    def __init__(self, cookies=None, headers=None):
        self.cookies = dict(cookies or {})
        self.headers = dict(headers or {})


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "server.db")


@pytest.fixture
def two_owners(store):
    """Create two local owners with passwords; return their (id, pw) tuples."""
    alice = store.create_owner("Alice", is_admin=True)
    store.link_identity(alice, "local", "alice")
    store.set_password(alice, crypto.hash_password("alice-pw"))

    bob = store.create_owner("Bob")
    store.link_identity(bob, "local", "bob")
    store.set_password(bob, crypto.hash_password("bob-pw"))
    return {"alice": (alice, "alice-pw"), "bob": (bob, "bob-pw")}


@pytest.fixture
def sessions():
    return SessionStore()


# --------------------------------------------------------------------------- #
# base: OwnerIdentity / ProviderChain                                         #
# --------------------------------------------------------------------------- #


class _StubProvider(AuthProvider):
    def __init__(self, name, identity, enabled=True):
        self.name = name
        self.enabled = enabled
        self._identity = identity

    def resolve_owner(self, request):
        return self._identity


def test_provider_chain_returns_first_resolution():
    a = _StubProvider("a", None)
    b = _StubProvider("b", OwnerIdentity("owner-b", provider="b"))
    c = _StubProvider("c", OwnerIdentity("owner-c", provider="c"))
    chain = ProviderChain([a, b, c])
    ident = chain.resolve_owner(FakeRequest())
    assert ident is not None and ident.owner_id == "owner-b"


def test_provider_chain_drops_disabled_and_is_falsy_when_empty():
    disabled = _StubProvider("x", OwnerIdentity("x"), enabled=False)
    chain = ProviderChain([disabled])
    assert len(chain) == 0
    assert not chain
    assert chain.resolve_owner(FakeRequest()) is None


# --------------------------------------------------------------------------- #
# session store                                                               #
# --------------------------------------------------------------------------- #


def test_session_create_resolve_and_hash_at_rest():
    clock = _Clock()
    store = SessionStore(ttl_seconds=100, now=clock)
    sid, session = store.create("owner-1")
    # High-entropy plaintext, recognizable prefix; only the hash is stored.
    assert sid and sid.startswith("ses_")
    assert session.id_hash == crypto.token_hash(sid)
    assert session.owner_id == "owner-1"
    # The plaintext id is not retained anywhere in the table.
    assert sid not in store._sessions
    resolved = store.resolve(sid)
    assert resolved is not None and resolved.owner_id == "owner-1"


def test_session_expiry_is_enforced():
    clock = _Clock()
    store = SessionStore(ttl_seconds=100, now=clock)
    sid, _ = store.create("owner-1")
    clock.advance(101)
    assert store.resolve(sid) is None
    # Expired session was evicted in passing.
    assert len(store) == 0


def test_session_resolve_rejects_unknown_and_empty():
    store = SessionStore()
    assert store.resolve(None) is None
    assert store.resolve("") is None
    assert store.resolve("ses_not-a-real-token") is None


def test_session_destroy_and_destroy_owner():
    store = SessionStore()
    sid1, _ = store.create("owner-1")
    sid2, _ = store.create("owner-1")
    assert store.destroy(sid1) is True
    assert store.destroy(sid1) is False  # idempotent
    assert store.resolve(sid2) is not None
    assert store.destroy_owner("owner-1") == 1
    assert store.resolve(sid2) is None


def test_session_auth_provider_resolves_from_cookie(store):
    oid = store.create_owner("Carol")
    sessions = SessionStore()
    provider = SessionAuthProvider(store, sessions)
    sid, _ = sessions.create(oid)
    req = FakeRequest(cookies={sessions.cookie_config.name: sid})
    ident = provider.resolve_owner(req)
    assert ident is not None and ident.owner_id == oid and ident.display_name == "Carol"
    # No cookie -> no identity (fail-closed, not anonymous).
    assert provider.resolve_owner(FakeRequest()) is None


def test_cookie_config_secure_defaults():
    cfg = CookieConfig()
    assert cfg.http_only is True
    assert cfg.secure is True
    assert cfg.same_site == "lax"


# --------------------------------------------------------------------------- #
# LocalAuthProvider                                                           #
# --------------------------------------------------------------------------- #


def test_local_login_authenticates_multiple_owners(store, two_owners, sessions):
    provider = LocalAuthProvider(store, sessions)
    alice_id, alice_pw = two_owners["alice"]
    bob_id, bob_pw = two_owners["bob"]

    res_a = provider.login("alice", alice_pw)
    res_b = provider.login("bob", bob_pw)
    assert res_a is not None and res_b is not None
    sid_a, ident_a = res_a
    sid_b, ident_b = res_b
    # Two distinct owners authenticated by the same built-in provider, no IdP.
    assert ident_a.owner_id == alice_id and ident_a.is_admin is True
    assert ident_b.owner_id == bob_id and ident_b.is_admin is False
    assert sid_a != sid_b

    # resolve_owner round-trips each session back to the right owner.
    assert provider.resolve_owner(
        FakeRequest(cookies={sessions.cookie_config.name: sid_a})
    ).owner_id == alice_id
    assert provider.resolve_owner(
        FakeRequest(cookies={sessions.cookie_config.name: sid_b})
    ).owner_id == bob_id


def test_local_login_rejects_bad_password_and_unknown_user(store, two_owners, sessions):
    provider = LocalAuthProvider(store, sessions)
    assert provider.login("alice", "wrong") is None
    assert provider.login("nobody", "whatever") is None


def test_local_login_rate_limit_locks_then_success_resets(store, two_owners, sessions):
    clock = _Clock()
    limiter = LoginRateLimiter(
        RateLimitConfig(max_failures=3, lockout_seconds=60, window_seconds=600),
        now=clock,
    )
    provider = LocalAuthProvider(store, sessions, rate_limiter=limiter)

    # Three consecutive failures trip the lockout.
    for _ in range(3):
        assert provider.login("alice", "wrong") is None
    with pytest.raises(LoginRateLimited) as exc:
        provider.login("alice", "alice-pw")  # locked: not even checked
    assert exc.value.retry_after > 0

    # After the lockout window, the correct password succeeds and resets state.
    clock.advance(61)
    res = provider.login("alice", "alice-pw")
    assert res is not None
    # Counter was reset on success: a single new failure does not re-lock.
    assert provider.login("alice", "wrong") is None
    assert limiter.is_locked("alice") is False


def test_local_credentials_and_session_never_logged(store, two_owners, sessions, caplog):
    provider = LocalAuthProvider(store, sessions)
    with caplog.at_level(logging.DEBUG, logger="se3.server"):
        res = provider.login("alice", "alice-pw")
        provider.login("alice", "super-secret-wrong-pw")
    assert res is not None
    sid, _ = res
    text = caplog.text
    assert "alice-pw" not in text
    assert "super-secret-wrong-pw" not in text
    assert sid not in text


# --------------------------------------------------------------------------- #
# ratelimit (unit)                                                            #
# --------------------------------------------------------------------------- #


def test_rate_limiter_window_forgets_old_failures():
    clock = _Clock()
    limiter = LoginRateLimiter(
        RateLimitConfig(max_failures=3, lockout_seconds=60, window_seconds=100),
        now=clock,
    )
    limiter.record_failure("k")
    limiter.record_failure("k")
    clock.advance(101)  # first two fall out of the window
    limiter.record_failure("k")
    assert limiter.is_locked("k") is False


# --------------------------------------------------------------------------- #
# registry: assembly, fail-closed, require_owner                             #
# --------------------------------------------------------------------------- #


def test_registry_default_is_local(store, sessions):
    chain = build_provider_chain(None, store=store, sessions=sessions)
    assert [p.name for p in chain.providers] == ["local"]


def test_registry_fail_closed_when_no_usable_provider(store, sessions):
    # Local explicitly disabled and nothing else enabled -> refuse to serve.
    with pytest.raises(AuthNotConfigured):
        build_provider_chain(
            {"providers": [{"type": "local", "enabled": False}]},
            store=store,
            sessions=sessions,
        )
    # An OIDC seam left unconfigured is disabled, so a chain of only-OIDC is empty.
    with pytest.raises(AuthNotConfigured):
        build_provider_chain(
            {"providers": ["oidc"]}, store=store, sessions=sessions
        )


def test_registry_can_switch_providers(store, sessions):
    # Enabling the proxy-header provider (with trust affirmed) selects it.
    chain = build_provider_chain(
        {
            "providers": [
                "local",
                {"type": "proxy_header", "enabled": True, "trust_proxy": True},
            ]
        },
        store=store,
        sessions=sessions,
    )
    assert [p.name for p in chain.providers] == ["local", "proxy_header"]


def test_require_owner_401_without_identity_and_passes_with(store, two_owners, sessions):
    from fastapi import HTTPException

    chain = build_provider_chain(None, store=store, sessions=sessions)
    require_owner = make_require_owner(chain)

    with pytest.raises(HTTPException) as exc:
        require_owner(FakeRequest())
    assert exc.value.status_code == 401

    # With a valid session cookie it returns the resolved identity.
    local = chain.providers[0]
    sid, _ = local.login("alice", "alice-pw")
    ident = require_owner(FakeRequest(cookies={sessions.cookie_config.name: sid}))
    assert ident.owner_id == two_owners["alice"][0]


# --------------------------------------------------------------------------- #
# seams: OIDC / proxy-header                                                  #
# --------------------------------------------------------------------------- #


def test_oidc_disabled_by_default_and_external_id_shape(store, sessions):
    provider = OidcProvider(None, store, sessions)
    assert provider.enabled is False
    assert provider.resolve_owner(FakeRequest()) is None
    # Configured-but-incomplete stays disabled (fail-closed).
    half = OidcProvider({"enabled": True, "issuer": "https://idp"}, store, sessions)
    assert half.enabled is False
    # issuer+sub is the stable external id mounted onto owner_id.
    assert oidc_external_id("https://idp", "sub-1") == "https://idp|sub-1"
    with pytest.raises(NotImplementedError):
        OidcProvider(None, store, sessions).begin_login(state="s", nonce="n")


def test_proxy_header_refuses_untrusted_header(store):
    oid = store.create_owner("Dana")
    store.link_identity(oid, "proxy_header", "dana@example.com")
    header = "x-forwarded-email"
    req = FakeRequest(headers={header: "dana@example.com"})

    # Default config: not trusted -> header ignored entirely.
    untrusted = ProxyHeaderProvider(None, store)
    assert untrusted.enabled is False
    assert untrusted.resolve_owner(req) is None

    # 'enabled' without affirming trust_proxy is still refused (fail-closed).
    no_trust = ProxyHeaderProvider({"enabled": True}, store)
    assert no_trust.enabled is False
    assert no_trust.resolve_owner(req) is None


def test_proxy_header_trusted_resolves_binding(store):
    oid = store.create_owner("Dana")
    store.link_identity(oid, "proxy_header", "dana@example.com")
    header = "x-forwarded-email"
    trusted = ProxyHeaderProvider({"enabled": True, "trust_proxy": True}, store)
    assert trusted.enabled is True
    # external_id -> owner_id path is reachable (schema not dug shut).
    ident = trusted.resolve_owner(FakeRequest(headers={header: "dana@example.com"}))
    assert ident is not None and ident.owner_id == oid
    # Unbound header value resolves to no owner (no implicit account creation).
    assert trusted.resolve_owner(
        FakeRequest(headers={header: "stranger@example.com"})
    ) is None
