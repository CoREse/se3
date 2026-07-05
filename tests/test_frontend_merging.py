"""Pytest bridge for the web console's worktree-merge sub-state (G5).

Once a ``--worktree`` run's flow body has COMPLETED, it is merged back into its
origin branch — possibly blocked queueing for the project's main-worktree lock.
During that window the flow snapshot carries ``merging=True`` (and, while queued
for the lock, ``waiting_for_lock=True``). The frontend folds that into a 合并中
indicator: the flow-list card badge overrides the terminal 已完成 with 合并中
(·等待主分支锁 while queued), and the chat transcript renders a 合并中 status
anchor. The indicator clears automatically once the flag disappears (a
successful merge archives the worktree engine.json).

The DOM-free pure-helper assertions (``isMerging`` / ``flowStatusLabel``) live in
``tests/frontend/merging.test.mjs``, which the Node assertion harness
``tests/frontend/test_app_pure.mjs`` loads and runs. This module pulls that
suite into the pytest run, asserts the G5 checks actually executed, and adds
static-source guardrails that the merging CSS exists and the render paths are
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
MERGING_TEST = REPO_ROOT / "tests" / "frontend" / "merging.test.mjs"


def test_merging_module_present():
    """The G5 mjs module exists and is registered into the harness."""
    assert MERGING_TEST.is_file(), f"missing {MERGING_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "merging.test.mjs" in harness, (
        "merging.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerMergingTests" in harness


def test_frontend_merging_node_suite_passes():
    """Run the Node assertion suite and confirm the G5 checks ran.

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
        "G5 isMerging true for a completed flow with the flag set",
        "G5 isMerging ignores a stale flag on an archived/history snapshot",
        "G5 flowStatusLabel overrides completed with 合并中",
        "G5 flowStatusLabel appends ·等待主分支锁 while queued for the lock",
        "G5 merging renders a 合并中 status row, not an empty bubble",
    ):
        assert needle in combined, (
            f"expected G5 check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined


def test_merging_css_present():
    """The merging chat-anchor and list-card badge selectors must exist."""
    assert STYLE_CSS.is_file(), f"missing {STYLE_CSS}"
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".conv-record.step-status-merging" in css, (
        "step-status-merging CSS is missing"
    )
    assert ".badge-merging" in css, "badge-merging CSS is missing"


def test_merging_render_paths_wired():
    """The flow card badge and the chat normalizer must use the G5 helpers."""
    assert APP_JS.is_file(), f"missing {APP_JS}"
    js = APP_JS.read_text(encoding="utf-8")
    assert "function isMerging(flow)" in js, "isMerging helper is missing"
    assert "isMerging(flow)" in js, (
        "renderFlowCard must override the badge on isMerging"
    )
    # The chat normalizer / dispatch / kind resolution must all recognise the
    # merging anchor so it renders as 合并中 rather than an empty bubble.
    assert 'eventType === "merging"' in js, (
        "normalizeRecord must recognise the merging status anchor"
    )
    assert 'norm.kind === "merging"' in js, (
        "the conversation dispatch must route the merging anchor"
    )
