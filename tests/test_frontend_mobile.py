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
        "G7 replyTextareaHeight clamps below-min up to minPx",
        "G7 replyTextareaHeight clamps above-max down to maxPx",
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


def test_mobile_block_reclaims_chat_horizontal_whitespace():
    """G1: the flow chat drops the redundant left bar + record padding and lets
    bubbles reach (near) full width, scoped under `.flow-conversation`."""
    block = _block_text(STYLE_CSS.read_text(encoding="utf-8"), MOBILE_BREAKPOINT_OPEN)
    # The 3px identity bar on the record is removed and its side padding zeroed.
    assert ".flow-conversation .history-record" in block, (
        "missing the flow-chat record override"
    )
    assert "border-left: none" in block, "the flow-chat record's left bar must be removed"
    # Outer padding narrowed from 16px to ~8px (top/bottom kept at 14px).
    assert "padding: 14px 8px" in block, "flow-conversation side padding must narrow to 8px"
    # Bubbles reach (near) full width — no 88% cap, no left/right offset.
    assert ".flow-conversation .conv-record.role-user .conv-bubble" in block
    assert ".flow-conversation .conv-record.role-assistant .conv-bubble" in block
    assert "max-width: 100%" in block, "chat bubbles must widen to (near) full width"
    assert "align-self: stretch" in block, "chat bubbles must stop offsetting left/right"


def test_mobile_block_compresses_tool_marker_to_one_line():
    """G2: the tool-call chip summary detail truncates to a single line, scoped
    under `.flow-conversation`. The expandable panel is left able to wrap."""
    block = _block_text(STYLE_CSS.read_text(encoding="utf-8"), MOBILE_BREAKPOINT_OPEN)
    assert ".flow-conversation .tool-marker-detail" in block, (
        "missing the mobile tool-marker detail override"
    )
    assert "white-space: nowrap" in block, "the chip detail must not wrap"
    assert "text-overflow: ellipsis" in block, "an over-long chip detail must ellipsis-truncate"
    # Root-cause fix: the detail's flex-basis must be 0 (not `auto`), otherwise a
    # long detail's hypothetical main size overflows the flex-wrap head line and
    # wraps onto a second row before the nowrap/ellipsis ever applies.
    _, _, detail_block = block.partition(".flow-conversation .tool-marker-detail")
    detail_rule = detail_block.split("}", 1)[0]
    assert "flex: 1 1 0" in detail_rule, (
        "the chip detail must use flex-basis 0 (flex: 1 1 0) so the head stays "
        "single-line; flex-basis auto wraps before truncating"
    )


def test_mobile_block_shares_call_chip_with_reply_head_row():
    """problem 2: a single call chip shares the docked reply-head row instead of
    claiming its own tall row, by flipping `.flow-reply` to a wrapping row so the
    chip bar and the reply-context (whose first line is `.flow-reply-head`) sit
    side by side, with the input row forced onto its own full-width line."""
    block = _block_text(STYLE_CSS.read_text(encoding="utf-8"), MOBILE_BREAKPOINT_OPEN)
    assert "#flow-view .flow-reply {" in block, (
        "missing the mobile docked-reply row layout"
    )
    _, _, reply_block = block.partition("#flow-view .flow-reply {")
    reply_rule = reply_block.split("}", 1)[0]
    assert "flex-direction: row" in reply_rule, (
        "the docked reply form must tile the chip bar and reply-context on one row"
    )
    assert "#flow-view .flow-reply .flow-interventions" in block, (
        "the chip bar must be sized to share the reply-head row"
    )
    assert "#flow-view .flow-reply .flow-reply-row" in block, (
        "the input row must be forced onto its own full-width line"
    )


def test_mobile_block_tiles_reply_meta_horizontally():
    """G2: the docked reply head + prompt toggle tile on one horizontal row,
    scoped under `#flow-view`; the prompt body / options drop to their own rows."""
    block = _block_text(STYLE_CSS.read_text(encoding="utf-8"), MOBILE_BREAKPOINT_OPEN)
    assert "#flow-view .flow-reply-context.active" in block, (
        "missing the mobile reply-meta tiling rule"
    )
    assert "flex-direction: row" in block, "reply meta must tile horizontally"


def test_mobile_block_textarea_is_auto_grow_not_fixed():
    """G3: the mobile reply textarea is a WeChat-style auto-grow box — capped at
    35vh with internal scroll, no manual resize, and the old fixed 104px min is
    gone."""
    block = _block_text(STYLE_CSS.read_text(encoding="utf-8"), MOBILE_BREAKPOINT_OPEN)
    assert "max-height: 35vh" in block, "the auto-grow textarea must cap at 35vh"
    assert "resize: none" in block, "the mobile textarea must drop the manual resize handle"
    assert "min-height: 104px" not in block, (
        "the old fixed mobile textarea min-height (104px) must be removed"
    )


def test_app_js_has_auto_grow_textarea_logic():
    """G3: app.js carries the pure clamp, the matchMedia-gated grower, and the
    `input` listener that drives the WeChat-style auto-grow."""
    js = APP_JS.read_text(encoding="utf-8")
    assert "function replyTextareaHeight" in js, "missing the pure clamp helper"
    assert "function autoGrowReplyTextarea" in js, "missing the auto-grow grower"
    assert '(max-width: 600px)' in js, "the grower must be matchMedia-gated to the breakpoint"
    # The input event must drive the grow as the user types.
    assert '"input", autoGrowReplyTextarea' in js, (
        "the auto-grow must be wired to the textarea's input event"
    )
    # Problem 4 root cause: the height must be pinned to 0 before measuring
    # scrollHeight, otherwise an empty / default field falls back to the
    # `rows="6"` intrinsic height and never collapses to a single line.
    _, _, grower = js.partition("function autoGrowReplyTextarea")
    grower_body = grower.split("\n}", 1)[0]
    assert 'input.style.height = "0px"' in grower_body, (
        "auto-grow must reset height to 0 (not 'auto') before measuring so the "
        "empty/default textarea collapses to a single line"
    )
    assert 'input.style.height = "auto"' not in grower_body, (
        "resetting to 'auto' regresses the empty field back to the 6-row height"
    )


def test_app_js_exports_reply_textarea_height():
    """The pure clamp is exported for the DOM-free mjs tests."""
    js = APP_JS.read_text(encoding="utf-8")
    start = js.find("module.exports")
    assert start != -1, "app.js has no module.exports block"
    assert "replyTextareaHeight" in js[start:], (
        "replyTextareaHeight is not exported for the pure tests"
    )


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
        # New visual rules from this pass — each unique to the mobile block, so
        # a leak to the top level (changing desktop) fails here.
        ".flow-conversation .tool-marker-detail",
        "#flow-view .flow-reply-context.active",
        "max-height: 35vh",
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
