"""Pytest bridge for the web console's PERIODIC progression-fallback retry (#260).

The discovery→analyze boundary can leave the WS push path silent for a whole
step. The prior fallback (8a128eb3) was a ONE-SHOT silent rebuild: it painted
only the disk state at the moment it fired (the lone analyze step label), so any
mid-step content the still-broken WS never pushed stayed invisible until the
reader exited and re-entered the session. The fix (G4) makes the grace timer
RE-ARM itself after each silent rebuild and keep pulling on the
``progressionGraceMs`` cadence until a genuine WS increment lands
(``flowConversationAppendSeq`` moves past the value frozen when the loop was
first armed) — so a WS that never recovers still surfaces freshly-written
mid-step content without an exit/re-enter, while the healthy path stays
zero-rebuild and the loop terminates the instant the push path returns.

The DOM-stub behavioral assertions live in
``tests/frontend/progression_fallback_retry.test.mjs``, which the Node assertion
harness ``tests/frontend/test_app_pure.mjs`` loads and runs. This module pulls
that suite into the pytest run, asserts the checks actually executed, and adds
static-source guardrails that the self-re-arming grace loop is wired into
``app.js``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "src" / "se3" / "server" / "static"
APP_JS = STATIC_DIR / "app.js"
FRONTEND_TEST = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"
RETRY_TEST = REPO_ROOT / "tests" / "frontend" / "progression_fallback_retry.test.mjs"


def test_progression_fallback_retry_module_present():
    """The G4 mjs module exists and is registered into the harness."""
    assert RETRY_TEST.is_file(), f"missing {RETRY_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "progression_fallback_retry.test.mjs" in harness, (
        "progression_fallback_retry.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerProgressionFallbackRetryTests" in harness


def test_frontend_progression_fallback_retry_node_suite_passes():
    """Run the Node assertion suite and confirm the periodic-retry checks ran.

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
        "periodic fallback: continuous WS silence keeps pulling growing mid-step content",
        "periodic fallback: a real WS increment stops the retry loop",
        "periodic fallback: a healthy WS within the first window → zero pulls",
        "periodic fallback: switching flows cancels the retry loop",
    ):
        assert needle in combined, (
            f"expected periodic-retry check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined


def test_progression_fallback_is_a_self_rearming_loop():
    """The fallback must be a self-re-arming periodic loop, not a one-shot: a
    dedicated ``armProgressionGrace`` helper must exist, be scheduled on the
    configurable ``progressionGraceMs`` window, and call itself again after the
    silent rebuild so a persistently-dead WS keeps surfacing mid-step content."""
    assert APP_JS.is_file(), f"missing {APP_JS}"
    js = APP_JS.read_text(encoding="utf-8")
    assert "function armProgressionGrace(" in js, (
        "a self-re-arming armProgressionGrace() helper must exist"
    )
    # The helper must re-arm itself (recursive call) so the loop persists while
    # the WS stays silent, and must schedule on the configurable grace window.
    assert js.count("armProgressionGrace(") >= 3, (
        "armProgressionGrace must be armed from the detector AND re-arm itself "
        "(a recursive call after the silent rebuild)"
    )
    assert "state.progressionGraceMs" in js, (
        "the loop must schedule on the configurable grace window"
    )
    # The loop must still rebuild through the silent full-reload path and gate on
    # the WS-increment counter so it stops the moment the push path recovers.
    assert "loadFlowConversation(flowId, { silent: true })" in js, (
        "the periodic loop must rebuild through the silent full-reload path"
    )
    assert "state.flowConversationAppendSeq > seqAtSchedule" in js, (
        "the loop must terminate when a genuine WS increment lands past the frozen snapshot"
    )
