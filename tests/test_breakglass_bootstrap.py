"""Tests for the break-glass bootstrap escape hatch.

Covers G4:

- ``bootstrap.py``: issue (plaintext printed once / stored hashed / re-issuable),
  constant-time + one-time + expiry-aware consume, and that the token plaintext
  never reaches the logging system.
- the ``se3-server bootstrap-token`` CLI subcommand and its fail-closed property
  (issuance does not import the ``[server]`` extra / FastAPI app).
- the provider-independent ``POST /api/auth/breakglass`` entry.
"""

from __future__ import annotations

import logging
import sys

import pytest

from se3.server import bootstrap, crypto
from se3.server.auth.session import SessionStore
from se3.server.persistence import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "server.db")
    yield s
    s.close()


# --------------------------------------------------------------------------- #
# issue / storage                                                             #
# --------------------------------------------------------------------------- #


def test_issue_returns_plaintext_and_stores_only_hash(store):
    token = bootstrap.issue_breakglass_token(store)
    assert token.startswith(bootstrap.BREAKGLASS_TOKEN_PREFIX + "_")

    # The DB holds the SHA-256 hash, never the plaintext.
    conn = store._conn()
    rows = conn.execute("SELECT token_hash FROM breakglass_tokens").fetchall()
    assert len(rows) == 1
    stored_hash = rows[0]["token_hash"]
    assert stored_hash == crypto.token_hash(token)
    assert stored_hash != token
    assert token not in stored_hash


def test_reissue_overwrites_prior_token(store):
    old = bootstrap.issue_breakglass_token(store)
    new = bootstrap.issue_breakglass_token(store)  # reissue=True default
    assert old != new

    conn = store._conn()
    count = conn.execute("SELECT COUNT(*) AS c FROM breakglass_tokens").fetchone()["c"]
    assert count == 1  # the old token row was purged

    # The old token no longer validates; only the freshly minted one does.
    assert bootstrap.consume_breakglass_token(store, old) is False
    assert bootstrap.consume_breakglass_token(store, new) is True


def test_keep_existing_does_not_purge(store):
    old = bootstrap.issue_breakglass_token(store)
    new = bootstrap.issue_breakglass_token(store, reissue=False)

    conn = store._conn()
    count = conn.execute("SELECT COUNT(*) AS c FROM breakglass_tokens").fetchone()["c"]
    assert count == 2
    # Both remain independently consumable.
    assert bootstrap.consume_breakglass_token(store, old) is True
    assert bootstrap.consume_breakglass_token(store, new) is True


# --------------------------------------------------------------------------- #
# consume: one-time / expiry / unknown                                        #
# --------------------------------------------------------------------------- #


def test_consume_is_one_time(store):
    token = bootstrap.issue_breakglass_token(store)
    assert bootstrap.consume_breakglass_token(store, token) is True
    # Consumed: a second attempt fails.
    assert bootstrap.consume_breakglass_token(store, token) is False


def test_consume_rejects_unknown_and_empty(store):
    bootstrap.issue_breakglass_token(store)
    assert bootstrap.consume_breakglass_token(store, "se3bg_not-a-real-token") is False
    assert bootstrap.consume_breakglass_token(store, "") is False
    assert bootstrap.consume_breakglass_token(store, None) is False


def test_expired_token_is_rejected(store):
    # Negative TTL ⇒ already expired at issue time.
    token = bootstrap.issue_breakglass_token(store, ttl_seconds=-1)
    assert bootstrap.consume_breakglass_token(store, token) is False


# --------------------------------------------------------------------------- #
# credential hygiene: token never logged                                      #
# --------------------------------------------------------------------------- #


def test_token_never_appears_in_logs(store, caplog):
    with caplog.at_level(logging.DEBUG):
        token = bootstrap.issue_breakglass_token(store)
        bootstrap.consume_breakglass_token(store, token)
        bootstrap.consume_breakglass_token(store, token)  # rejected path too
    assert token not in caplog.text
    # The random secret part specifically must not leak.
    secret_part = token.split("_", 1)[1]
    assert secret_part not in caplog.text


def test_announcement_carries_token_but_is_returned_not_logged(store, caplog):
    token = bootstrap.issue_breakglass_token(store)
    with caplog.at_level(logging.DEBUG):
        announcement = bootstrap.format_announcement(token)
    # The plaintext is in the human-facing announcement (printed once)...
    assert token in announcement
    # ...but building it logs nothing containing the token.
    assert token not in caplog.text


# --------------------------------------------------------------------------- #
# break-glass admin owner + full login                                        #
# --------------------------------------------------------------------------- #


def test_ensure_breakglass_admin_is_idempotent_admin(store):
    oid1 = bootstrap.ensure_breakglass_admin(store)
    oid2 = bootstrap.ensure_breakglass_admin(store)
    assert oid1 == oid2 == bootstrap.BREAKGLASS_ADMIN_OWNER_ID
    owner = store.get_owner(oid1)
    assert owner is not None and owner.is_admin is True
    # Only one admin owner is created.
    assert len(store.list_owners()) == 1


def test_consume_login_mints_admin_session(store):
    sessions = SessionStore()
    token = bootstrap.issue_breakglass_token(store)

    result = bootstrap.consume_breakglass_login(store, sessions, token)
    assert result is not None
    session_id, identity = result
    assert identity.owner_id == bootstrap.BREAKGLASS_ADMIN_OWNER_ID
    assert identity.is_admin is True
    # The minted session resolves back to the admin owner.
    resolved = sessions.resolve(session_id)
    assert resolved is not None and resolved.owner_id == identity.owner_id

    # One-time: reusing the token does not mint another session.
    assert bootstrap.consume_breakglass_login(store, sessions, token) is None


# --------------------------------------------------------------------------- #
# CLI: `se3-server bootstrap-token`                                           #
# --------------------------------------------------------------------------- #


def test_cli_prints_token_and_stores_hash(tmp_path, capsys):
    db = tmp_path / "server.db"
    rc = bootstrap.run_bootstrap_token_cli(["--db-path", str(db)])
    assert rc == 0
    out = capsys.readouterr().out

    # Exactly one token printed to stdout; its hash is what's persisted.
    s = Store(db)
    rows = s._conn().execute("SELECT token_hash FROM breakglass_tokens").fetchall()
    assert len(rows) == 1
    stored_hash = rows[0]["token_hash"]
    # The printed announcement contains the plaintext whose hash is stored.
    printed_tokens = [
        w for w in out.split() if w.startswith(bootstrap.BREAKGLASS_TOKEN_PREFIX + "_")
    ]
    assert len(printed_tokens) == 1
    assert crypto.token_hash(printed_tokens[0]) == stored_hash
    s.close()


def test_cli_reissue_overwrites(tmp_path, capsys):
    db = tmp_path / "server.db"
    bootstrap.run_bootstrap_token_cli(["--db-path", str(db)])
    bootstrap.run_bootstrap_token_cli(["--db-path", str(db)])
    s = Store(db)
    count = s._conn().execute(
        "SELECT COUNT(*) AS c FROM breakglass_tokens"
    ).fetchone()["c"]
    assert count == 1
    s.close()


def test_main_bootstrap_token_runs_before_server_extra_import(tmp_path):
    """The escape hatch must not require the [server] extra / FastAPI app.

    Issuing a token through ``se3.server.main(["bootstrap-token", ...])`` must
    not import ``se3.server.app`` (the FastAPI/uvicorn-bearing module), proving
    the interception happens before the extra check.
    """
    import se3.server as server_pkg

    # Drop any previously-imported app module so we can detect a fresh import.
    sys.modules.pop("se3.server.app", None)

    db = tmp_path / "server.db"
    with pytest.raises(SystemExit) as exc:
        server_pkg.main(["bootstrap-token", "--db-path", str(db)])
    assert exc.value.code == 0
    assert "se3.server.app" not in sys.modules

    # And the token really was minted.
    s = Store(db)
    count = s._conn().execute(
        "SELECT COUNT(*) AS c FROM breakglass_tokens"
    ).fetchone()["c"]
    assert count == 1
    s.close()


def test_main_version_flag_exits_zero(capsys):
    import se3.server as server_pkg

    with pytest.raises(SystemExit) as exc:
        server_pkg.main(["--version"])
    assert exc.value.code == 0
    assert "se3-server version" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# endpoint: provider-independent POST /api/auth/breakglass                    #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client_with_store(tmp_path):
    from fastapi.testclient import TestClient

    from se3.server.app import create_app

    store = Store(tmp_path / "server.db")
    sessions = SessionStore()
    app = create_app(store=store, sessions=sessions)
    with TestClient(app) as client:
        yield client, store, sessions
    store.close()


def test_breakglass_endpoint_accepts_valid_token(client_with_store):
    client, store, sessions = client_with_store
    token = bootstrap.issue_breakglass_token(store)

    assert len(sessions) == 0
    resp = client.post("/api/auth/breakglass", json={"token": token})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["admin"] is True
    # A session was minted and a Set-Cookie header was returned.
    assert len(sessions) == 1
    assert "set-cookie" in {k.lower() for k in resp.headers.keys()}

    # One-time: a replay of the same token is rejected.
    resp2 = client.post("/api/auth/breakglass", json={"token": token})
    assert resp2.status_code == 401


def test_breakglass_endpoint_rejects_bad_token(client_with_store):
    client, store, _sessions = client_with_store
    bootstrap.issue_breakglass_token(store)
    resp = client.post("/api/auth/breakglass", json={"token": "se3bg_nope"})
    assert resp.status_code == 401


def test_breakglass_endpoint_rejects_empty_token(client_with_store):
    client, _store, _sessions = client_with_store
    resp = client.post("/api/auth/breakglass", json={"token": "   "})
    assert resp.status_code == 422
