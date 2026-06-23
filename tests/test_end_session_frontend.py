"""Pytest bridge for the web console's End-session controls (Group G4).

The End-session feature adds an "End" button to flow cards and the flow detail
sidebar that — after a confirmation modal — POSTs ``/api/flows/{id}/end`` to
terminate (and, for worktree sessions, archive) a non-completed flow. The pure
gate ``isFlowEndable(flow)`` decides whether the button is shown.

The behavioural assertions for the DOM-free pure helpers live in the standalone
Node suite ``tests/frontend/end_session.test.mjs`` (same pattern as
``tests/frontend/flow_resume.test.mjs``). This pytest module:
  1. runs that Node suite and asserts the key ``isFlowEndable`` branches
     actually executed (not silently skipped);
  2. statically guards that ``app.js`` carries and exports the new helpers,
     ``index.html`` carries the confirmation modal, and ``style.css`` carries
     the ``.btn-end`` style.

The Node suite is skipped when ``node`` is not available on PATH; it is still
runnable by hand via ``node tests/frontend/end_session.test.mjs``.
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
INDEX_HTML = STATIC_DIR / "index.html"
END_SESSION_TEST = REPO_ROOT / "tests" / "frontend" / "end_session.test.mjs"


# ---------------------------------------------------------------------------
# 1. Node suite — the pure helpers actually run and pass
# ---------------------------------------------------------------------------
def test_end_session_module_present():
    """The standalone mjs suite exists."""
    assert END_SESSION_TEST.is_file(), f"missing {END_SESSION_TEST}"


def test_frontend_end_session_node_suite_passes():
    """Run the Node assertion suite and confirm the end-session checks ran.

    Skipped if ``node`` is not available on PATH; the suite is still runnable
    by hand via ``node tests/frontend/end_session.test.mjs``.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    result = subprocess.run(
        [node, str(END_SESSION_TEST)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"end-session test runner exited {result.returncode}:\n{combined}"
    )
    # The full isFlowEndable decision matrix must have actually executed.
    for needle in (
        "running flow with flow_id is endable",
        "paused flow is endable",
        "failed flow is endable",
        "completed flow is not endable",
        "archived flow is not endable even when running",
        "history flow is not endable even when failed",
        "flow without flow_id is not endable",
        "null flow is not endable",
        "isEndInProgress returns true when flow is in the set",
    ):
        assert needle in combined, (
            f"expected end-session check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined


# ---------------------------------------------------------------------------
# 2. app.js static guards — helpers exist and are exported
# ---------------------------------------------------------------------------
def test_app_js_has_end_session_helpers():
    js = APP_JS.read_text(encoding="utf-8")
    for fn in (
        "function isFlowEndable",
        "function isEndInProgress",
        "function makeEndButton",
        "function endFlow",
        "function openEndSessionModal",
        "function closeEndSessionModal",
        "function confirmEndSession",
    ):
        assert fn in js, f"app.js is missing the end-session helper {fn!r}"
    # The debounce set must be declared on the shared state object.
    assert "endSessionRequests" in js, "state.endSessionRequests is missing"


def test_app_js_exports_end_session_pure_helpers():
    js = APP_JS.read_text(encoding="utf-8")
    start = js.find("module.exports")
    assert start != -1, "app.js has no module.exports block"
    exports = js[start:]
    for name in ("isFlowEndable", "isEndInProgress", "makeEndButton"):
        assert name in exports, f"{name} is not exported for the pure tests"


def test_app_js_end_button_injected_into_card_and_sidebar():
    """The End button is wired into both the flow card and the detail sidebar."""
    js = APP_JS.read_text(encoding="utf-8")
    # makeEndButton must be invoked at least twice (card + sidebar).
    assert js.count("makeEndButton(flow)") >= 2, (
        "makeEndButton must be injected into both the flow card and the sidebar"
    )


def test_app_js_end_flow_handles_all_receipts():
    """endFlow must branch on the honest receipts from POST /api/flows/{id}/end."""
    js = APP_JS.read_text(encoding="utf-8")
    start = js.find("async function endFlow")
    assert start != -1, "endFlow function missing"
    body = js[start : start + 1600]
    assert "/end" in body, "endFlow must POST to the /end endpoint"
    assert "404" in body, "endFlow must handle 404"
    assert "409" in body, "endFlow must handle 409"
    assert "503" in body, "endFlow must handle 503"
    assert "endSessionRequests" in body, "endFlow must debounce via endSessionRequests"


# ---------------------------------------------------------------------------
# 3. index.html static guards — the confirmation modal exists
# ---------------------------------------------------------------------------
def test_index_html_has_end_session_modal():
    html = INDEX_HTML.read_text(encoding="utf-8")
    for ident in (
        'id="end-session-modal"',
        'id="end-session-confirm"',
        'id="end-session-cancel"',
        'id="end-session-close"',
        'id="end-session-message"',
        'id="end-session-error"',
    ):
        assert ident in html, f"index.html is missing the end-session control {ident}"
    # The modal must default to hidden, like every other modal.
    start = html.find('id="end-session-modal"')
    modal_head = html[start - 40 : start + 60]
    assert "modal hidden" in modal_head, "end-session-modal must default to hidden"


# ---------------------------------------------------------------------------
# 4. style.css static guard — the .btn-end style exists and is distinct
# ---------------------------------------------------------------------------
def test_style_css_has_btn_end():
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".btn-end {" in css, "style.css is missing the .btn-end rule"
    start = css.find(".btn-end {")
    rule = css[start : css.find("}", start) + 1]
    # The End button uses the danger (red) accent so it reads distinctly from
    # the Resume button (which uses --accent).
    assert "var(--red)" in rule, ".btn-end must use the danger (--red) accent"
    assert ".btn-end:disabled" in css, "missing the disabled-state rule for .btn-end"
