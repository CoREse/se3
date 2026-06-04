"""Pytest bridge for the mobile-portrait responsive pass (Group G7).

The mobile pass (G1–G6) hardens the web console for phone-portrait use entirely
inside narrow-screen breakpoints, so the desktop layout stays byte-for-byte
unchanged. The behavioural assertions for the DOM-free pure helpers
(navMenuNextState / listPanelState / historyPanelState / flowSidebarNextState)
live in ``tests/frontend/mobile_responsive.test.mjs``, which the Node assertion
harness ``tests/frontend/test_app_pure.mjs`` loads and runs.

This pytest module:
  1. pulls the Node suite into the pytest run and asserts the G7 mobile checks
     actually executed (not silently skipped);
  2. adds static-source guards over ``style.css`` that the key mobile rules of
     each interface exist — a phone breakpoint, a horizontal-scroll guard,
     minimum touch-target heights, iOS-safe form font sizing, the flow-view
     off-canvas drawer, near-full-screen modals, and the topbar overflow menu;
  3. asserts ``index.html`` carries the mobile toggle / back controls and
     ``app.js`` carries the matching switch functions;
  4. asserts the new *visual* mobile rules live INSIDE media queries, the
     regression guard that protects the desktop layout from any change.
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
FRONTEND_TEST = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"
MOBILE_TEST = REPO_ROOT / "tests" / "frontend" / "mobile_responsive.test.mjs"

MOBILE_BREAKPOINT_HEADER = "@media (max-width: 600px)"
# The literal block opener (header + brace) — disambiguates the real block
# from the same string appearing inside the breakpoint-strategy comment.
MOBILE_BREAKPOINT_OPEN = "@media (max-width: 600px) {"


# ---------------------------------------------------------------------------
# helpers: brace-balanced @media range extraction
# ---------------------------------------------------------------------------
def _media_ranges(css: str) -> list[tuple[int, int]]:
    """Return (start, end) char ranges of every ``@media`` block (brace-matched).

    Nested blocks resolve to the outermost @media's range, which is all the
    containment guard needs — a rule inside ANY media query is desktop-safe.
    """
    ranges: list[tuple[int, int]] = []
    i = 0
    while True:
        start = css.find("@media", i)
        if start == -1:
            break
        brace = css.find("{", start)
        if brace == -1:
            break
        depth = 0
        j = brace
        while j < len(css):
            ch = css[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        ranges.append((start, j))
        i = j + 1
    return ranges


def _inside_media(idx: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= idx <= end for start, end in ranges)


def _block_text(css: str, header: str) -> str:
    """Return the brace-balanced body text of the block introduced by ``header``."""
    start = css.find(header)
    assert start != -1, f"missing CSS block header {header!r}"
    brace = css.find("{", start)
    depth = 0
    j = brace
    while j < len(css):
        ch = css[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return css[brace : j + 1]


# ---------------------------------------------------------------------------
# 1. mjs bridge — the pure-helper suite actually runs
# ---------------------------------------------------------------------------
def test_mobile_responsive_module_present_and_registered():
    """The G7 mjs module exists and is wired into the Node harness."""
    assert MOBILE_TEST.is_file(), f"missing {MOBILE_TEST}"
    harness = FRONTEND_TEST.read_text(encoding="utf-8")
    assert "mobile_responsive.test.mjs" in harness, (
        "mobile_responsive.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerMobileResponsiveTests" in harness


def test_frontend_mobile_node_suite_passes():
    """Run the Node assertion suite and confirm the G7 mobile checks ran.

    Skipped if ``node`` is not available on PATH; the suite is still runnable by
    hand via ``node tests/frontend/test_app_pure.mjs``.
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
    # The G7 mobile checks must have actually executed across all four helpers,
    # not silently been skipped.
    for needle in (
        "G7 navMenuNextState toggles the open flag",
        "G7 flowSidebarNextState toggles the open flag",
        "G7 listPanelState: select-machine → flows",
        "G7 historyPanelState: select-session → detail",
    ):
        assert needle in combined, (
            f"expected G7 check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined


# ---------------------------------------------------------------------------
# 2. style.css static guards — each interface's key mobile rules exist
# ---------------------------------------------------------------------------
def test_style_has_phone_portrait_breakpoint():
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert MOBILE_BREAKPOINT_HEADER in css, (
        "the phone-portrait breakpoint @media (max-width: 600px) is missing"
    )


def test_mobile_block_has_horizontal_scroll_guard():
    """The phone breakpoint clamps the document so nothing forces a sideways scroll."""
    block = _block_text(STYLE_CSS.read_text(encoding="utf-8"), MOBILE_BREAKPOINT_OPEN)
    assert "overflow-x: hidden" in block, "missing global horizontal-scroll guard"
    assert "max-width: 100%" in block, "missing viewport width clamp"


def test_mobile_block_has_touch_targets_and_safe_form_fonts():
    """Interactive controls get a finger-friendly min-height; form fields ≥16px."""
    block = _block_text(STYLE_CSS.read_text(encoding="utf-8"), MOBILE_BREAKPOINT_OPEN)
    assert "min-height: 40px" in block, "missing ~40px touch-target floor"
    # 16px form fields suppress iOS focus auto-zoom.
    assert "font-size: 16px" in block, "form fields must be pinned to 16px"


def test_mobile_block_has_panel_switches():
    """The main list and History collapse to single-view panel switches."""
    block = _block_text(STYLE_CSS.read_text(encoding="utf-8"), MOBILE_BREAKPOINT_OPEN)
    # Main-list Machines↔Flows (G3) and History list↔detail (G5) switches.
    assert ".layout.active-flows" in block, "missing main-list panel switch"
    assert ".history-view.active-detail" in block, "missing History panel switch"


def test_mobile_block_has_flow_view_off_canvas_drawer():
    """The running console's sidebar becomes a transform-based off-canvas drawer."""
    block = _block_text(STYLE_CSS.read_text(encoding="utf-8"), MOBILE_BREAKPOINT_OPEN)
    assert ".flow-sidebar" in block, "missing flow-view sidebar drawer rule"
    assert "transform: translateX(-100%)" in block, (
        "the sidebar must be parked off-canvas via a negative translateX"
    )
    assert ".flow-view.sidebar-open .flow-sidebar" in block, (
        "missing the .sidebar-open slide-in rule"
    )
    assert ".flow-sidebar-backdrop" in block, "missing the drawer backdrop rule"


def test_mobile_block_has_near_full_screen_modals():
    """New Task / Daemon Keys / 用户管理 modals go near full-screen on a phone."""
    block = _block_text(STYLE_CSS.read_text(encoding="utf-8"), MOBILE_BREAKPOINT_OPEN)
    assert ".modal-card" in block, "missing modal full-screen rule"
    assert "width: 100vw" in block, "modal card must span the full viewport width"
    # 100dvh tracks the dynamic viewport (URL bar); 100vh is the fallback.
    assert "100dvh" in block, "modal card should use 100dvh for the dynamic viewport"


def test_mobile_block_has_topbar_overflow_menu():
    """The topbar functional controls collapse behind the hamburger overflow menu."""
    block = _block_text(STYLE_CSS.read_text(encoding="utf-8"), MOBILE_BREAKPOINT_OPEN)
    assert ".nav-menu-toggle" in block, "missing hamburger toggle rule"
    assert ".nav-menu.open" in block, "missing the open-dropdown rule for the nav menu"


# ---------------------------------------------------------------------------
# 3. index.html controls + app.js switch functions exist
# ---------------------------------------------------------------------------
def test_index_html_has_mobile_controls():
    html = INDEX_HTML.read_text(encoding="utf-8")
    for ident in (
        'id="nav-menu-toggle"',
        'id="nav-menu"',
        'id="flow-sidebar-toggle"',
        'id="flow-sidebar-backdrop"',
        'id="flows-back-btn"',
        'id="history-back-btn"',
    ):
        assert ident in html, f"index.html is missing the mobile control {ident}"


def test_app_js_has_switch_functions():
    js = APP_JS.read_text(encoding="utf-8")
    for fn in (
        # topbar overflow menu (G2)
        "toggleNavMenu",
        "closeNavMenu",
        "navMenuNextState",
        # flow-view drawer (G4)
        "toggleFlowSidebar",
        "closeFlowSidebar",
        "flowSidebarNextState",
        # panel switches (G3 / G5)
        "applyListPanelAction",
        "listPanelState",
        "applyHistoryPanelAction",
        "historyPanelState",
    ):
        assert fn in js, f"app.js is missing the mobile switch function {fn}"


def test_app_js_exports_pure_helpers():
    """The four pure state helpers are exported for the DOM-free mjs tests."""
    js = APP_JS.read_text(encoding="utf-8")
    # The module.exports block at the foot of app.js (Node-only) must surface
    # every helper the mjs suite asserts against.
    start = js.find("module.exports")
    assert start != -1, "app.js has no module.exports block"
    exports = js[start:]
    for name in (
        "navMenuNextState",
        "listPanelState",
        "historyPanelState",
        "flowSidebarNextState",
    ):
        assert name in exports, f"{name} is not exported for the pure tests"


# ---------------------------------------------------------------------------
# 4. desktop-protection guard — mobile visual rules live inside media queries
# ---------------------------------------------------------------------------
def test_mobile_visual_rules_are_inside_media_queries():
    """Every mobile-introduced selector must appear ONLY inside an @media block.

    This is the regression guard that protects the desktop layout: because none
    of these rules can match a wide viewport, the desktop console stays
    byte-for-byte unchanged. A selector unique to the mobile pass leaking to the
    top level (outside any media query) would change desktop and must fail here.
    """
    css = STYLE_CSS.read_text(encoding="utf-8")
    ranges = _media_ranges(css)
    assert ranges, "expected at least one @media block in style.css"

    # Selectors / tokens introduced solely by the mobile pass. Each occurrence
    # must fall inside a media query. (Base desktop rules like a bare
    # `.flow-sidebar {` are intentionally NOT in this list — they legitimately
    # live at the top level.)
    mobile_only_tokens = (
        ".nav-menu.open",
        "active-flows",
        "active-detail",
        "sidebar-open",
        "translateX(-100%)",
        "100dvh",
    )
    for token in mobile_only_tokens:
        idx = css.find(token)
        assert idx != -1, f"expected mobile token {token!r} somewhere in style.css"
        while idx != -1:
            assert _inside_media(idx, ranges), (
                f"mobile rule {token!r} at offset {idx} is OUTSIDE a media query — "
                f"it would change the desktop layout"
            )
            idx = css.find(token, idx + 1)


def test_media_range_extraction_is_balanced():
    """Sanity-check the brace matcher: the 600px block body is well-formed.

    Guards the guard itself — if _block_text mis-counted braces the other
    style.css assertions would silently read the wrong slice.
    """
    block = _block_text(STYLE_CSS.read_text(encoding="utf-8"), MOBILE_BREAKPOINT_OPEN)
    assert block.startswith("{") and block.endswith("}")
    assert block.count("{") == block.count("}"), "unbalanced braces in the 600px block"
