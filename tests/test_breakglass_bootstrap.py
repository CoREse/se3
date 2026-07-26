"""Tests for the break-glass bootstrap escape hatch (group G10 task 2).

Covers ``tianluo.server.bootstrap`` and the ``se3-server bootstrap-token``
subcommand:

* issuance mints a fresh token, persists only its hash, and returns/prints the
  plaintext exactly once (re-issuable, one-time consumption, optional TTL);
* the security baseline — the plaintext token never lands in the on-disk store
  nor in any log output;
* dependency isolation — ``bootstrap-token`` (like ``--version``) is intercepted
  *before* the ``[server]`` extra import, so it works on a core-only install
  with FastAPI / uvicorn / argon2 absent;
* the bootstrap → live-server seam: a token minted by the CLI against a sqlite
  file is consumable by a server opened on that same file.
"""

from __future__ import annotations

import io
import logging
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

#: This worktree's ``src`` dir — propagated to spawned interpreters so they
#: import the code under test rather than any globally installed ``se3``.
_SRC = str(Path(__file__).resolve().parent.parent / "src")

import tianluo.server.crypto as crypto
from tianluo.server import bootstrap
from tianluo.server.persistence import Store

#: The minted plaintext always carries the break-glass token prefix.
_TOKEN_RE = re.compile(r"bg_[A-Za-z0-9_\-]+")


# --------------------------------------------------------------------------
# issue_breakglass_token — hash at rest, one-time, re-issuable, TTL
# --------------------------------------------------------------------------


def test_issue_returns_plaintext_and_persists_only_the_hash():
    store = Store(":memory:")
    plaintext, token_id = bootstrap.issue_breakglass_token(store)
    assert plaintext.startswith("bg_")
    assert token_id
    # The stored row holds the hash, which is consumable; the plaintext itself
    # was never persisted (consume works by hashing the presented plaintext).
    assert store.consume_breakglass(crypto.token_hash(plaintext)) is True


def test_issued_token_is_one_time():
    store = Store(":memory:")
    plaintext, _ = bootstrap.issue_breakglass_token(store)
    h = crypto.token_hash(plaintext)
    assert store.consume_breakglass(h) is True
    # A consumed token cannot be replayed.
    assert store.consume_breakglass(h) is False


def test_issue_is_reissuable_with_distinct_tokens():
    store = Store(":memory:")
    p1, id1 = bootstrap.issue_breakglass_token(store)
    p2, id2 = bootstrap.issue_breakglass_token(store)
    assert p1 != p2 and id1 != id2
    # Both outstanding tokens are independently valid (re-issue does not revoke).
    assert store.consume_breakglass(crypto.token_hash(p1)) is True
    assert store.consume_breakglass(crypto.token_hash(p2)) is True


def test_issue_honours_ttl_expiry():
    store = Store(":memory:")
    # A token that expired one hour ago is rejected by consume.
    plaintext, _ = bootstrap.issue_breakglass_token(store, ttl_seconds=-3600)
    assert store.consume_breakglass(crypto.token_hash(plaintext)) is False
    # A generous TTL is still consumable.
    fresh, _ = bootstrap.issue_breakglass_token(store, ttl_seconds=3600)
    assert store.consume_breakglass(crypto.token_hash(fresh)) is True


# --------------------------------------------------------------------------
# print_breakglass_token — single console reveal, never logged / never on disk
# --------------------------------------------------------------------------


def test_print_reveals_token_once_to_the_stream(tmp_path):
    db = tmp_path / "server.db"
    out = io.StringIO()
    returned = bootstrap.print_breakglass_token(str(db), stream=out)
    printed = out.getvalue()
    assert returned in printed
    # Exactly one reveal of the secret in the console banner.
    assert printed.count(returned) == 1
    # The printed token is the real, consumable one.
    store = Store(str(db))
    assert store.consume_breakglass(crypto.token_hash(returned)) is True


def test_plaintext_token_never_written_to_the_store_file(tmp_path):
    db = tmp_path / "server.db"
    out = io.StringIO()
    token = bootstrap.print_breakglass_token(str(db), stream=out)
    # Scan every sqlite artifact (db + WAL/SHM) for the plaintext: only the
    # SHA-256 hash is allowed to touch disk.
    for path in tmp_path.glob("server.db*"):
        blob = path.read_bytes()
        assert token.encode("utf-8") not in blob, f"plaintext leaked into {path.name}"


def test_issuance_never_logs_the_plaintext(tmp_path, caplog):
    db = tmp_path / "server.db"
    out = io.StringIO()
    with caplog.at_level(logging.DEBUG):
        token = bootstrap.print_breakglass_token(str(db), stream=out)
    # The console stream carries the secret (by design); the logs never do.
    assert token in out.getvalue()
    assert token not in caplog.text


# --------------------------------------------------------------------------
# se3-server bootstrap-token CLI
# --------------------------------------------------------------------------


def test_cli_bootstrap_token_prints_token_and_exits_zero(tmp_path, capsys):
    from tianluo import server

    db = tmp_path / "server.db"
    with pytest.raises(SystemExit) as exc:
        server.main(["bootstrap-token", "--db-path", str(db)])
    assert exc.value.code == 0

    printed = capsys.readouterr().out
    match = _TOKEN_RE.search(printed)
    assert match, f"no break-glass token printed:\n{printed}"
    token = match.group(0)
    # The CLI-minted token is real and consumable from the same store file.
    store = Store(str(db))
    assert store.consume_breakglass(crypto.token_hash(token)) is True


def test_cli_bootstrap_token_is_reissuable(tmp_path, capsys):
    from tianluo import server

    db = tmp_path / "server.db"
    tokens = []
    for _ in range(2):
        with pytest.raises(SystemExit) as exc:
            server.main(["bootstrap-token", "--db-path", str(db)])
        assert exc.value.code == 0
        tokens.append(_TOKEN_RE.search(capsys.readouterr().out).group(0))
    assert tokens[0] != tokens[1]
    store = Store(str(db))
    assert store.consume_breakglass(crypto.token_hash(tokens[0])) is True
    assert store.consume_breakglass(crypto.token_hash(tokens[1])) is True


# --------------------------------------------------------------------------
# dependency isolation: bootstrap-token works on a core-only install
# --------------------------------------------------------------------------


def _run_python(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        env=env,
    )


def test_bootstrap_token_works_without_server_extra(tmp_path):
    """`se3-server bootstrap-token` mints a token even with the extra absent.

    Like ``--version``, the subcommand is intercepted before FastAPI / uvicorn
    are imported, so a core-only install can still bootstrap the escape hatch.
    """
    db = tmp_path / "server.db"
    code = f"""
        import builtins, sys
        _real_import = builtins.__import__

        def _blocked(name, *args, **kwargs):
            top = name.split(".")[0]
            if top in ("fastapi", "uvicorn", "argon2", "bcrypt"):
                raise ImportError("simulated core-only install: " + name)
            return _real_import(name, *args, **kwargs)

        builtins.__import__ = _blocked
        from tianluo.server import main
        try:
            main(["bootstrap-token", "--db-path", {str(db)!r}])
        except SystemExit as exc:
            print("EXITCODE", exc.code)
        assert "fastapi" not in sys.modules, "fastapi leaked on the bootstrap path"
    """
    proc = _run_python(code)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "EXITCODE 0" in proc.stdout
    assert "ImportError" not in proc.stderr
    assert "Traceback (most recent call last)" not in proc.stderr
    assert _TOKEN_RE.search(proc.stdout), f"no token printed:\n{proc.stdout}"


def test_server_version_works_without_server_extra():
    """`se3-server --version` is honored before the extra import (core-only)."""
    from tianluo import __version__

    code = """
        import builtins, sys
        _real_import = builtins.__import__

        def _blocked(name, *args, **kwargs):
            if name.split(".")[0] in ("fastapi", "uvicorn"):
                raise ImportError("simulated core-only install: " + name)
            return _real_import(name, *args, **kwargs)

        builtins.__import__ = _blocked
        from tianluo.server import main
        try:
            main(["--version"])
        except SystemExit as exc:
            print("EXITCODE", exc.code)
    """
    proc = _run_python(code)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "EXITCODE 0" in proc.stdout
    assert f"tianluo-server version {__version__}" in proc.stdout


@pytest.mark.parametrize("module", ["tianluo.cli", "tianluo.daemon", "tianluo.daemon.daemon"])
def test_core_modules_import_clean(module):
    """core-only install: importing tianluo.cli / tianluo.daemon never errors."""
    proc = _run_python(
        f"""
        import {module}  # must import with no server extra present
        print("OK")
        """
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "OK" in proc.stdout


# --------------------------------------------------------------------------
# bootstrap -> live server seam: a CLI-minted token logs an admin in
# --------------------------------------------------------------------------


def test_cli_token_is_consumable_by_a_server_on_the_same_db(tmp_path, capsys):
    from fastapi.testclient import TestClient

    from tianluo import server
    from tianluo.server.app import create_app
    from tianluo.server.auth.session import CookieConfig, SessionStore

    db = tmp_path / "server.db"
    with pytest.raises(SystemExit):
        server.main(["bootstrap-token", "--db-path", str(db)])
    token = _TOKEN_RE.search(capsys.readouterr().out).group(0)

    # A server opened on the same store file accepts the CLI-minted token and
    # mints an admin session (the bootstrap entrance).
    app = create_app(
        db_path=str(db),
        session_store=SessionStore(cookie_config=CookieConfig(secure=False)),
    )
    with TestClient(app) as client:
        ok = client.post("/api/auth/breakglass", json={"token": token})
        assert ok.status_code == 200 and ok.json()["is_admin"] is True
        # One-time: the same token cannot be replayed against the server.
        replay = client.post("/api/auth/breakglass", json={"token": token})
        assert replay.status_code == 401
