"""Pytest bridge for the element-anchored scroll-preservation fix (issue #217).

issue #209's progression-triggered SILENT conversation rebuild restored an
absolute pixel ``scrollTop`` after a from-scratch ``renderConversation(append=
false)``. Because re-laying-out the same records can give the content ABOVE the
reader's viewport a different total height, that absolute restore scrolled the
conversation up a large stretch — the bug issue #217 reports. The fix anchors on
the bubble the reader is looking at (``captureScrollAnchor``) and, after the
rebuild, moves ``scrollTop`` so that same bubble (matched by ``recordKey`` across
the old/new arrays) returns to the same viewport offset (``restoreScrollAnchor``),
absorbing any height change above it.

The DOM-stub behavioral assertions live in
``tests/frontend/issue217_scroll_anchor.test.mjs``, which the Node assertion
harness ``tests/frontend/test_app_pure.mjs`` loads and runs. This module pulls
that suite into the pytest run, asserts the anchor checks actually executed, and
adds static-source guardrails that the anchor helpers are wired into ``app.js``.
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
ANCHOR_TEST = REPO_ROOT / "tests" / "frontend" / "issue217_scroll_anchor.test.mjs"


def test_anchor_module_present_and_registered():
    """The anchor mjs module exists and is wired into the harness."""
    assert ANCHOR_TEST.is_file(), f"missing {ANCHOR_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "issue217_scroll_anchor.test.mjs" in harness, (
        "issue217_scroll_anchor.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerIssue217ScrollAnchorTests" in harness


def test_frontend_anchor_node_suite_passes():
    """Run the Node assertion suite and confirm the anchor checks ran.

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
        "anchor: bubble stays at the same viewport offset when content above grows",
        "anchor: re-finds the bubble by recordKey when a record is inserted ahead",
        "anchor: capture returns null when geometry is unavailable",
        "anchor: restore falls back to the clamped absolute scrollTop",
    ):
        assert needle in combined, (
            f"expected anchor check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined


def test_anchor_helpers_wired_into_silent_rebuild():
    """The silent rebuild must capture an anchor before the rebuild and restore
    it after, instead of the old absolute-pixel ``scrollTop`` assignment."""
    assert APP_JS.is_file(), f"missing {APP_JS}"
    js = APP_JS.read_text(encoding="utf-8")
    assert "function captureScrollAnchor(" in js, (
        "the element anchor capture helper must exist"
    )
    assert "function restoreScrollAnchor(" in js, (
        "the element anchor restore helper must exist"
    )
    assert "captureScrollAnchor(container, state.flowConversationRecords)" in js, (
        "the silent branch must capture the anchor from the pre-merge records"
    )
    assert "restoreScrollAnchor(" in js, (
        "the silent branch must restore the anchor after the rebuild"
    )
    # The old direct absolute-pixel restore must be gone — it is now the fallback
    # inside restoreScrollAnchor, not an inline assignment in loadFlowConversation.
    assert "container.scrollTop = Math.min(preserveScrollTop, container.scrollHeight)" not in js, (
        "the inline absolute-pixel scrollTop restore must be replaced by the anchor restore"
    )
