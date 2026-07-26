"""Pytest bridge for the discovery→analyze silent-rebuild scroll fix (issue #260).

At the discovery→analyze boundary the WS increment stalls, so content lands
without an auto-scroll (or a large chunk arrives between the ``isNearBottom``
measure and the scroll). The progression fallback then fires a SILENT full
rebuild whose stickiness used to be decided by the FROZEN-DOM ``isNearBottom`` —
which reads ``scrollHeight-scrollTop-clientHeight>80`` and MISJUDGES a
bottom-follower as scrolled-up, so the rebuild took the anchor branch, pinned the
old tail, and jumped the view up. The fix reads a persistent
``flowConversationFollowingBottom`` intent — driven only by real scroll /
scroll-to-bottom signals — so a follower who merely drifted from a stalled
append still sticks to the bottom, while a genuinely scrolled-up reader keeps
their element-anchored viewport offset.

The DOM-stub behavioral assertions live in
``tests/frontend/discovery_analyze_scroll_anchor.test.mjs``, which the Node
assertion harness ``tests/frontend/test_app_pure.mjs`` loads and runs. This
module pulls that suite into the pytest run, asserts the checks actually
executed, and adds static-source guardrails that the intent flag is wired into
``app.js``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "src" / "tianluo" / "server" / "static"
APP_JS = STATIC_DIR / "app.js"
FRONTEND_TEST = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"
SCROLL_TEST = REPO_ROOT / "tests" / "frontend" / "discovery_analyze_scroll_anchor.test.mjs"


def test_scroll_module_present_and_registered():
    """The discovery→analyze scroll mjs module exists and is wired into the harness."""
    assert SCROLL_TEST.is_file(), f"missing {SCROLL_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "discovery_analyze_scroll_anchor.test.mjs" in harness, (
        "discovery_analyze_scroll_anchor.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerDiscoveryAnalyzeScrollAnchorTests" in harness


def test_frontend_scroll_node_suite_passes():
    """Run the Node assertion suite and confirm the boundary scroll checks ran.

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
        "discovery→analyze: a bottom-follower sticks to the new bottom",
        "discovery→analyze: a scrolled-up reader keeps the anchored bubble",
    ):
        assert needle in combined, (
            f"expected boundary scroll check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined


def test_following_bottom_intent_wired_into_silent_stick():
    """The silent rebuild must decide stickiness from the persistent
    follow-bottom intent (not solely the point-in-time frozen ``isNearBottom``),
    and that intent must be maintained by the scroll / scroll-to-bottom paths."""
    assert APP_JS.is_file(), f"missing {APP_JS}"
    js = APP_JS.read_text(encoding="utf-8")
    assert "flowConversationFollowingBottom" in js, (
        "the persistent follow-bottom intent field must exist"
    )
    # The silent stick decision must consult the intent flag.
    assert "state.flowConversationFollowingBottom || isNearBottom(container)" in js, (
        "the silent stick decision must OR the follow-bottom intent with the frozen measurement"
    )
    # A user scroll of the conversation must update the intent.
    assert 'scroller.id === "flow-conversation"' in js, (
        "the conversation scroll handler must maintain the follow-bottom intent"
    )
    # Landing at the bottom (re)establishes the intent.
    assert "state.flowConversationFollowingBottom = true" in js, (
        "scroll-to-bottom / open must re-arm the follow-bottom intent"
    )
