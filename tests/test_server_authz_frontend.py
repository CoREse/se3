"""Pytest bridge for the web console's auth / owner-narrowing pure helpers (G9).

The multi-tenant control plane gates the SPA behind a login view: an
unauthenticated visitor sees the sign-in ceremony, an authenticated owner sees
only its own machines/flows/history, and a 401 from any ``/api/*`` call kicks
the session back to the login gate. The DOM-free logic behind that — the
``nextAuthState`` login state machine, ``ownerLabel``, the 401 predicate, the
owner-narrowing ``visibleMachinesForOwner`` / ``canOwnerControlMachine``, and
the ``daemonKeyRowModel`` view model — is exercised by the Node assertion suite
``tests/test_server_authz_frontend.mjs`` (isomorphic node-stub tests, avoiding
the chromium e2e path that fails here for lack of libnspr4.so).

This module pulls that suite into the pytest run and adds a couple of cheap
static guards on the login / daemon-key markup so the wiring cannot silently
disappear.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "src" / "se3" / "server" / "static"
APP_JS = STATIC_DIR / "app.js"
INDEX_HTML = STATIC_DIR / "index.html"
FRONTEND_TEST = REPO_ROOT / "tests" / "test_server_authz_frontend.mjs"


def test_authz_frontend_module_present():
    """The G9 node-stub suite exists and exports the tested pure helpers."""
    assert FRONTEND_TEST.is_file(), f"missing {FRONTEND_TEST}"
    app_src = APP_JS.read_text(encoding="utf-8")
    for symbol in (
        "nextAuthState",
        "ownerLabel",
        "isUnauthorizedStatus",
        "visibleMachinesForOwner",
        "canOwnerControlMachine",
        "daemonKeyRowModel",
    ):
        assert symbol in app_src, f"{symbol} missing from app.js"


def test_login_and_keys_markup_present():
    """The login gate and daemon-key panel markup are wired into index.html."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    # Login gate.
    assert 'id="login-view"' in html
    assert 'id="login-form"' in html
    assert 'id="breakglass-form"' in html
    # Top-bar owner / logout.
    assert 'id="owner-label"' in html
    assert 'id="logout-btn"' in html
    # Daemon-key panel.
    assert 'id="keys-modal"' in html
    assert 'id="keys-create-form"' in html
    assert 'id="keys-reveal"' in html


def test_authz_frontend_node_suite_passes():
    """Run the Node assertion suite and confirm the G9 checks ran.

    Skipped if ``node`` is not available on PATH; the suite is still runnable by
    hand via ``node tests/test_server_authz_frontend.mjs``.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    result = subprocess.run(
        [node, str(FRONTEND_TEST)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"frontend test runner exited {result.returncode}:\n{combined}"
    )
    for needle in (
        "G9 success events transition to authed",
        "G9 a regular owner controls only its own machines",
        "G9 daemonKeyRowModel marks a revoked key",
    ):
        assert needle in combined, (
            f"expected G9 check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined
