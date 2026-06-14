"""Pytest bridge for the web console's waiting-for-lock running sub-state (G2).

A synchronous run queued behind the project's main-worktree mutex stays
RUNNING but carries ``waiting_for_lock=True`` on its flow snapshot. The
frontend folds that into a running·waiting-for-lock indicator (a chip on the
flow card plus the ``flowStatusLabel`` shown in the flow detail overview) so the
flow reads as running rather than appearing to stall on "已发布"; the indicator
clears automatically once the flag flips back to false.

The DOM-free pure-helper assertions (``isWaitingForLock`` / ``flowStatusLabel``)
live in ``tests/frontend/waiting_for_lock.test.mjs``, which the Node assertion
harness ``tests/frontend/test_app_pure.mjs`` loads and runs. This module pulls
that suite into the pytest run, asserts the G2 checks actually executed, and
adds static-source guardrails that the chip CSS exists and the render paths are
wired.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "src" / "se3" / "server" / "static"
APP_JS = STATIC_DIR / "app.js"
STYLE_CSS = STATIC_DIR / "style.css"
FRONTEND_TEST = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"
WAITING_TEST = REPO_ROOT / "tests" / "frontend" / "waiting_for_lock.test.mjs"


def test_waiting_for_lock_module_present():
    """The G2 mjs module exists and is registered into the harness."""
    assert WAITING_TEST.is_file(), f"missing {WAITING_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "waiting_for_lock.test.mjs" in harness, (
        "waiting_for_lock.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerWaitingForLockTests" in harness


def test_frontend_waiting_for_lock_node_suite_passes():
    """Run the Node assertion suite and confirm the G2 checks ran.

    Skipped if ``node`` is not available on PATH; runnable by hand via
    ``node tests/frontend/test_app_pure.mjs``.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    assert FRONTEND_TEST.is_file(), f"missing {FRONTEND_TEST}"
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
        "G2 isWaitingForLock true for a running flow with the flag set",
        "G2 isWaitingForLock ignores a stale flag on a terminal flow",
        "G2 flowStatusLabel folds waiting-for-lock into the running label",
    ):
        assert needle in combined, (
            f"expected G2 check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined


def test_waiting_for_lock_chip_css_present():
    """The waiting-for-lock chip selector must exist."""
    assert STYLE_CSS.is_file(), f"missing {STYLE_CSS}"
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".badge-waiting-lock" in css, "badge-waiting-lock CSS is missing"


def test_waiting_for_lock_render_paths_wired():
    """The flow card chip and detail-overview label must use the G2 helpers."""
    assert APP_JS.is_file(), f"missing {APP_JS}"
    js = APP_JS.read_text(encoding="utf-8")
    assert "isWaitingForLock(flow)" in js, (
        "renderFlowCard must gate the waiting-for-lock chip on isWaitingForLock"
    )
    assert "badge-waiting-lock" in js, (
        "renderFlowCard must render the badge-waiting-lock chip"
    )
    assert "flowStatusLabel(flow)" in js, (
        "the flow detail overview must show the folded status label"
    )
