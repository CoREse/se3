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
STATIC_DIR = REPO_ROOT / "src" / "tianluo" / "server" / "static"
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


def test_mobile_block_tool_marker_toggle_strips_native_appearance():
    """Defect 1: the folded tool-call card stays tall on real mobile WebKit even
    after the min-height/line-height/padding relaxation, because the toggle is a
    native `<button>` (`appearance: auto`) whose intrinsic vertical control
    metrics those declarations cannot remove. The fix adds
    `-webkit-appearance: none; appearance: none;` (scoped to the mobile
    breakpoint) so the button collapses to its text height. Also assert the
    parent chip re-centers its head line so the flattened button can't re-inflate
    the row via baseline alignment."""
    block = _block_text(STYLE_CSS.read_text(encoding="utf-8"), MOBILE_BREAKPOINT_OPEN)
    assert ".flow-conversation .tool-marker-toggle {" in block, (
        "missing the mobile tool-marker toggle override"
    )
    _, _, toggle_block = block.partition(".flow-conversation .tool-marker-toggle {")
    toggle_rule = toggle_block.split("}", 1)[0]
    assert "-webkit-appearance: none" in toggle_rule, (
        "the toggle must strip the native control with -webkit-appearance: none "
        "(the real root cause of the still-tall card on mobile WebKit)"
    )
    assert "appearance: none" in toggle_rule, (
        "the toggle must strip the native control with appearance: none"
    )
    assert "min-height: auto" in toggle_rule, (
        "the toggle must keep relaxing the 40px touch-target floor"
    )
    # The parent chip re-centers its head line on the cross axis so the compacted
    # toggle never drives the row height via baseline math.
    assert ".flow-conversation .tool-marker {" in block, (
        "missing the mobile .tool-marker baseline-row adjustment"
    )
    _, _, marker_block = block.partition(".flow-conversation .tool-marker {")
    marker_rule = marker_block.split("}", 1)[0]
    assert "align-items: center" in marker_rule, (
        "the mobile chip head must center its line so the flattened toggle "
        "cannot re-inflate the row height via baseline alignment"
    )


def test_desktop_tool_marker_toggle_rule_unchanged():
    """Defect 1 hard constraint: the desktop (non-media) `.tool-marker-toggle`
    rule must be byte-for-byte unchanged — the mobile fix lives strictly inside
    the breakpoint. Pin the exact desktop body and assert it carries no
    `appearance` declaration (which would mean the fix leaked to desktop)."""
    css = STYLE_CSS.read_text(encoding="utf-8")
    start = css.find("\n.tool-marker-toggle {")
    assert start != -1, "desktop .tool-marker-toggle rule is missing"
    brace = css.find("{", start)
    depth = 0
    j = brace
    while j < len(css):
        if css[j] == "{":
            depth += 1
        elif css[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    desktop_body = css[brace : j + 1]
    expected = (
        "{\n"
        "  margin-left: auto;\n"
        "  flex-shrink: 0;\n"
        "  background: transparent;\n"
        "  border: 1px solid rgba(160, 160, 160, 0.4);\n"
        "  border-radius: 3px;\n"
        "  color: var(--fg-dim);\n"
        "  cursor: pointer;\n"
        "  font-family: inherit;\n"
        "  font-size: 10.5px;\n"
        "  padding: 1px 6px;\n"
        "}"
    )
    assert desktop_body == expected, (
        "the desktop .tool-marker-toggle rule changed — the mobile defect-1 fix "
        "must be scoped strictly inside the @media (max-width: 600px) breakpoint"
    )
    assert "appearance" not in desktop_body, (
        "appearance:none must NOT appear on the desktop toggle rule"
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


def test_mobile_block_hardens_history_overflow():
    """History mobile overflow hardening: the two panes carry min-width: 0 +
    overflow-x: hidden, the project-select is shrinkable (flex: 1 1 0), the
    item-meta wraps, and long text (step-title, msg-chip) breaks-word. None of
    these rules use overflow-x: auto."""
    block = _block_text(STYLE_CSS.read_text(encoding="utf-8"), MOBILE_BREAKPOINT_OPEN)

    # --- container shrink layer ---
    # The two panes must carry min-width: 0 and overflow-x: hidden so they
    # clip rather than introduce a horizontal scrollbar.
    for pane_sel in (
        "#history-view .history-list-pane",
        "#history-view .history-detail-pane",
    ):
        assert pane_sel in block, (
            f"missing container-shrink rule for {pane_sel!r}"
        )
        _, _, after = block.partition(pane_sel)
        rule_body = after.split("}", 1)[0]
        assert "min-width: 0" in rule_body, (
            f"{pane_sel} must carry min-width: 0"
        )
        assert "overflow-x: hidden" in rule_body, (
            f"{pane_sel} must carry overflow-x: hidden"
        )
        assert "overflow-x: auto" not in rule_body, (
            f"{pane_sel} must NOT use overflow-x: auto"
        )

    # history-detail and history-step also need min-width: 0 + max-width: 100%
    for sel in (
        "#history-view .history-detail",
        "#history-view .history-step",
    ):
        assert sel in block, f"missing shrink rule for {sel!r}"
        _, _, after = block.partition(sel)
        rule_body = after.split("}", 1)[0]
        assert "min-width: 0" in rule_body, (
            f"{sel} must carry min-width: 0"
        )

    # --- long-content wrapping layer ---
    # Project select: flex-basis 0 + min-width 0 (same root cause as
    # .tool-marker-detail). Use exact selector with trailing " {" to avoid
    # matching the -row variant.
    select_sel = "#history-view .history-project-select {"
    assert select_sel in block, (
        "missing project-select shrink rule"
    )
    _, _, sel_block = block.partition(select_sel)
    sel_rule = sel_block.split("}", 1)[0]
    assert "flex: 1 1 0" in sel_rule, (
        "project-select must use flex-basis 0 (flex: 1 1 0)"
    )
    assert "min-width: 0" in sel_rule, (
        "project-select must carry min-width: 0"
    )

    # Project select row: flex-wrap: wrap
    row_sel = "#history-view .history-project-select-row {"
    assert row_sel in block, (
        "missing project-select-row rule"
    )
    _, _, row_block = block.partition(row_sel)
    row_rule = row_block.split("}", 1)[0]
    assert "flex-wrap: wrap" in row_rule, (
        "project-select-row must carry flex-wrap: wrap"
    )

    # Item meta: flex-wrap: wrap + overflow-wrap on children.
    # Use exact selector with trailing " {" to avoid matching the > span variant.
    meta_sel = "#history-view .history-item-meta {"
    assert meta_sel in block, (
        "missing item-meta rule"
    )
    _, _, meta_block = block.partition(meta_sel)
    meta_rule = meta_block.split("}", 1)[0]
    assert "flex-wrap: wrap" in meta_rule, (
        "item-meta must carry flex-wrap: wrap"
    )
    assert "#history-view .history-item-meta > span" in block, (
        "missing item-meta > span wrapping rule"
    )
    _, _, span_block = block.partition("#history-view .history-item-meta > span")
    span_rule = span_block.split("}", 1)[0]
    assert "overflow-wrap: anywhere" in span_rule or "word-break: break-word" in span_rule, (
        "item-meta > span must carry a per-character break rule"
    )

    # Step title and msg-chip: overflow-wrap / word-break
    for text_sel in (
        "#history-view .history-step-title",
        "#history-view .msg-chip",
    ):
        assert text_sel in block, (
            f"missing wrapping rule for {text_sel!r}"
        )
        _, _, after = block.partition(text_sel)
        rule_body = after.split("}", 1)[0]
        assert "overflow-wrap: anywhere" in rule_body or "word-break: break-word" in rule_body, (
            f"{text_sel} must carry a per-character break rule"
        )
        assert "overflow-x: auto" not in rule_body, (
            f"{text_sel} must NOT use overflow-x: auto"
        )


# The report-card constructs that carry no wrapping declaration of their own at
# the top level, so they depend entirely on inheriting one from the card root.
# Listed here so a new construct added without hardening shows up as a gap.
STEP_REPORT_CONSTRUCTS = (
    "step-report__status-bar",
    "step-report__stat",
    "step-report__title",
    "step-report__label",
    "step-report__section-title",
    "step-report__kv-row",
    "step-report__kv-nested",
    "step-report__kv-k",
    "step-report__warn",
    "step-report__empty",
    "step-report__muted",
    "step-report__file-dir",
    "step-report__conv-turn",
    "step-report__markdown",
)


def test_mobile_block_hardens_flow_conversation_overflow():
    """The running console's conversation must not scroll sideways on a phone.

    Root cause of the reported "流程界面可以左右滑动": `.flow-conversation` declares
    `overflow-y: auto`, which per CSS makes its computed `overflow-x` `auto` too,
    so the conversation is its own scroll container and the mobile
    `html, body { overflow-x: hidden }` backstop never sees the overflow.
    Meanwhile every `.step-report__*` construct is authored at the top level with
    no wrapping declaration, so a no-space token (a real analyze `outputs.scope`
    in this repo's archive carries an 83-character run) cannot be broken and the
    ANALYZE scope stat blows the column open.
    """
    block = _block_text(STYLE_CSS.read_text(encoding="utf-8"), MOBILE_BREAKPOINT_OPEN)

    # (a) The conversation scroll container is pinned closed on the x axis.
    sel = "#flow-view .flow-conversation {"
    assert sel in block, (
        "missing the mobile #flow-view .flow-conversation containment rule"
    )
    _, _, after = block.partition(sel)
    rule = after.split("}", 1)[0]
    assert "overflow-x: hidden" in rule, (
        "the conversation scroll container must pin overflow-x: hidden — its "
        "overflow-y: auto otherwise computes overflow-x to auto and lets the "
        "user swipe the console sideways"
    )

    # (b) Long-content wrapping on the conversation record + step separators.
    #     overflow-wrap/word-break inherit, so this one rule reaches every
    #     step-report construct, markdown node, .msg-chip and anonymous inline
    #     box inside a record.
    # Anchored on the two-selector prefix, which is unique to this rule —
    # `.conv-record` alone also opens the older G4 containment rule.
    wrap_sel = (
        "#flow-view .flow-conversation .conv-record,\n"
        "  #flow-view .flow-conversation .history-step-header,"
    )
    assert wrap_sel in block, "missing the flow-view conversation wrapping rule"
    idx = block.find(wrap_sel)
    wrap_rule = block[idx : block.find("}", idx)]
    for needed in (
        "#history-view .history-detail .conv-record",
        "#history-view .history-detail .history-step-header",
    ):
        assert needed in wrap_rule, (
            f"{needed} must share the conversation wrapping rule so the History "
            f"view (which renders the same cards) wraps instead of clipping"
        )
    assert "overflow-wrap: anywhere" in wrap_rule, (
        "the wrapping rule must use overflow-wrap: anywhere — `break-word` alone "
        "does not reduce the min-content contribution, so the flex item stays "
        "wider than the column"
    )
    assert "word-break: break-word" in wrap_rule, (
        "keep word-break: break-word alongside for legacy-WebKit parity, "
        "matching the #history-view hardening precedent"
    )

    # (c) Shrink protection along the card's flex chain.
    shrink_sel = "#flow-view .flow-conversation .step-report,"
    assert shrink_sel in block, "missing the report-card shrink-protection rule"
    idx = block.find(shrink_sel)
    shrink_rule = block[idx : block.find("}", idx)]
    for construct in (
        "step-report__head",
        "step-report__body",
        "step-report__status-bar",
        "step-report__section",
        "step-report__kv-row",
        "step-report__kv-nested",
    ):
        for prefix in ("#flow-view .flow-conversation", "#history-view .history-detail"):
            assert f"{prefix} .{construct}" in shrink_rule, (
                f"{prefix} .{construct} must carry min-width: 0 / max-width: 100%"
            )
    assert "min-width: 0" in shrink_rule and "max-width: 100%" in shrink_rule

    # The kv key column's desktop `min-width: 100px` alignment floor is a hard
    # shrink stop on a phone; the breakpoint relaxes it.
    kv_sel = "#flow-view .flow-conversation .step-report__kv-k,"
    assert kv_sel in block, "missing the mobile kv-key min-width relaxation"
    kv_rule = block[block.find(kv_sel) : block.find("}", block.find(kv_sel))]
    assert "min-width: 0" in kv_rule

    # No horizontal-scroll escape hatch is introduced anywhere in this pass:
    # content WRAPS (the Mobile Horizontal-Overflow contract).
    section = block[block.find("conversation long-content / step-report card") :]
    section = section[: section.find("idle reply placeholder")]
    assert "overflow-x: auto" not in section, (
        "the report-card hardening must never add a horizontal-scroll escape hatch"
    )


def test_mobile_block_bounds_the_nested_kv_indent_ladder():
    """Nested generic outputs must not indent themselves out of the column.

    `renderGenericKvRow` recurses over `step.outputs` with no depth limit, and a
    desktop `.step-report__kv-nested` level costs 16px margin + 8px padding +
    1px border. Because the wrapper is a `flex-basis: 100%` item, that indent is
    SUBTRACTIVE — every level shrinks the usable column. Measured at 320px the
    innermost row runs out of width around the twelfth level, and wrapping
    cannot rescue a column with no width left: the key/value paint outside the
    card and the overflow backstop clips them. The breakpoint therefore both
    narrows one level and stops the ladder accruing past the fourth, so total
    indentation is a constant at any depth the renderer can reach.
    """
    block = _block_text(STYLE_CSS.read_text(encoding="utf-8"), MOBILE_BREAKPOINT_OPEN)

    # (a) One level is narrower on a phone than on the desktop ladder. Anchored
    #     on the flow+history selector PAIR — `.step-report__kv-nested` alone
    #     also opens the shrink-protection rule above.
    narrow_sel = (
        "#flow-view .flow-conversation .step-report__kv-nested,\n"
        "  #history-view .history-detail .step-report__kv-nested {"
    )
    assert narrow_sel in block, (
        "missing the mobile nested-kv indent rule shared by the Flow and "
        "History views (both render the same generic-outputs card)"
    )
    idx = block.find(narrow_sel)
    narrow_rule = block[idx : block.find("}", idx)]
    for decl in ("margin-left: 8px", "padding-left: 6px"):
        assert decl in narrow_rule, f"the mobile nested-kv rule must set {decl}"

    # (b) The ladder STOPS accruing past a fixed depth. The cap is expressed as
    #     a self-descendant chain, so it matches every deeper level too and the
    #     total indent stays bounded rather than proportional to the data.
    unit = ".step-report__kv-nested"
    cap_head = f"#flow-view .flow-conversation {unit} {unit}"
    assert cap_head in block, (
        "missing the nested-kv depth cap — a selector chaining "
        f"{unit} onto itself, without which indentation grows with the data"
    )
    idx = block.find(cap_head)
    cap_rule = block[idx : block.find("}", idx)]
    assert f"#history-view .history-detail {unit} {unit}" in cap_rule, (
        "the History view must share the depth cap"
    )
    # Five chained units per view (four ancestors + the matched element), i.e.
    # the cap engages at the fifth level and every level below it.
    assert cap_rule.count(unit) >= 10, (
        f"the depth cap must chain five {unit} per view; found "
        f"{cap_rule.count(unit)} occurrences across both views"
    )
    for decl in ("margin-left: 0", "padding-left: 0", "border-left: none"):
        assert decl in cap_rule, f"the depth-cap rule must reset {decl}"

    # The cap is a mobile-only overlay: the desktop ladder keeps its full
    # per-level offset (the base rule outside every media query).
    css = STYLE_CSS.read_text(encoding="utf-8")
    ranges = _media_ranges(css)
    base = css.find(".step-report__kv-nested {")
    assert base != -1 and not _inside_media(base, ranges), (
        "the base nested-kv rule must stay outside the breakpoints"
    )
    base_rule = css[base : css.find("}", base)]
    assert "margin-left: 16px" in base_rule and "padding-left: 8px" in base_rule, (
        "desktop indentation must stay unchanged by this pass"
    )


def test_mobile_step_report_constructs_are_covered_by_inherited_wrapping():
    """Every unhardened `.step-report__*` construct sits under `.conv-record`.

    The fix relies on inheritance: one `overflow-wrap` declaration on the
    conversation record reaches all of them. That only holds while the report
    card is actually rendered inside a `.conv-record`, so assert app.js still
    builds the card that way — otherwise the CSS guard above is vacuous.
    """
    js = APP_JS.read_text(encoding="utf-8")
    _, _, body = js.partition("function renderStepEventRecord(norm) {")
    assert body, "renderStepEventRecord is missing from app.js"
    body = body[: body.find("\nfunction ")]
    assert '"history-record conv-record role-step-event kind-"' in body, (
        "the step-report card's row must stay a .conv-record so the mobile "
        "wrapping rule reaches the card by inheritance"
    )
    # And each construct the sweep covers must still be produced by app.js —
    # a renamed class would silently escape the hardening.
    for construct in STEP_REPORT_CONSTRUCTS:
        assert construct in js, (
            f"{construct} is no longer emitted by app.js; re-check the mobile "
            f"overflow sweep before deleting it from STEP_REPORT_CONSTRUCTS"
        )


def test_mobile_block_releases_the_constructs_that_opt_out_of_wrapping():
    """Inheritance cannot reach a construct that overrides the property itself.

    The conversation-wide `overflow-wrap: anywhere` above is inherited, so it
    covers a construct only while that construct does not set the competing
    property locally. Three do, at the top level, and each carries exactly the
    kind of unbreakable identifier this pass is about:

      * `.agent-badge` pins `white-space: nowrap` (a long configured runner name
        plus a long model id then keeps the whole inline-block wider than the
        bubble — `white-space` beats any inherited `overflow-wrap`);
      * `.tool-marker-name` is a `flex-shrink: 0` flex item, so a structured
        `mcp__<server>__<tool>` name pins the chip open;
      * `.tool-marker-input-key` is `flex-shrink: 0` inside the detail panel,
        where an MCP tool's argument names are arbitrary.

    Without the release below they merely vanish under the `overflow-x: hidden`
    backstop (Flow) or the pane clip (History) — silently truncated content,
    which is what containment is there to prevent, not to substitute for.
    """
    block = _block_text(STYLE_CSS.read_text(encoding="utf-8"), MOBILE_BREAKPOINT_OPEN)

    badge_sel = "#flow-view .flow-conversation .agent-badge,"
    assert badge_sel in block, "missing the mobile agent-badge wrapping release"
    idx = block.find(badge_sel)
    badge_rule = block[idx : block.find("}", idx)]
    assert "#history-view .history-detail .agent-badge" in badge_rule, (
        "the History view renders the same badge and must share the release"
    )
    assert "white-space: normal" in badge_rule, (
        "the badge's desktop `white-space: nowrap` must be released, or the "
        "inherited overflow-wrap can never take effect"
    )
    for decl in ("max-width: 100%", "overflow-wrap: anywhere", "word-break: break-word"):
        assert decl in badge_rule, f"the agent-badge release must carry {decl}"

    chip_sel = "#flow-view .flow-conversation .tool-marker-name,"
    assert chip_sel in block, "missing the mobile tool-chip shrink release"
    idx = block.find(chip_sel)
    chip_rule = block[idx : block.find("}", idx)]
    for construct in ("tool-marker-name", "tool-marker-input-key",
                      "tool-marker-glyph", "tool-marker-toggle"):
        for prefix in ("#flow-view .flow-conversation", "#history-view .history-detail"):
            assert f"{prefix} .{construct}" in chip_rule, (
                f"{prefix} .{construct} is a non-shrinking chip-head/detail item "
                f"and must be released on mobile"
            )
    for decl in ("flex-shrink: 1", "min-width: 0",
                 "overflow-wrap: anywhere", "word-break: break-word"):
        assert decl in chip_rule, f"the tool-chip release must carry {decl}"

    # `.tool-marker-detail` is deliberately NOT released: its mobile one-line
    # ellipsis is the chip summary's design (the full text is one tap away in
    # the details panel), and it is asserted by the rule further up this block.
    assert ".flow-conversation .tool-marker-detail" in block
    detail_idx = block.find(".flow-conversation .tool-marker-detail")
    detail_rule = block[detail_idx : block.find("}", detail_idx)]
    assert "white-space: nowrap" in detail_rule and "text-overflow: ellipsis" in detail_rule


def test_desktop_keeps_the_badge_and_chip_head_rigid():
    """The release is mobile-only: desktop keeps its one-line pill and columns."""
    css = STYLE_CSS.read_text(encoding="utf-8")
    ranges = _media_ranges(css)

    start = css.find("\n.agent-badge {")
    assert start != -1, "the desktop .agent-badge rule is missing"
    assert not _inside_media(start + 1, ranges)
    assert "white-space: nowrap" in css[start : css.find("}", start)], (
        "the desktop badge must stay on one line"
    )
    for sel in ("\n.tool-marker-name {", "\n.tool-marker-input-key {"):
        start = css.find(sel)
        assert start != -1, f"the desktop {sel.strip()} rule is missing"
        assert not _inside_media(start + 1, ranges)
        assert "flex-shrink: 0" in css[start : css.find("}", start)], (
            f"{sel.strip()} must keep its desktop non-shrinking behaviour"
        )

    for token in (
        "#flow-view .flow-conversation .agent-badge",
        "#flow-view .flow-conversation .tool-marker-name",
        "#history-view .history-detail .agent-badge",
        "#history-view .history-detail .tool-marker-name",
    ):
        idx = css.find(token)
        assert idx != -1, f"expected {token!r} in style.css"
        while idx != -1:
            assert _inside_media(idx, ranges), (
                f"{token!r} at offset {idx} is OUTSIDE a media query"
            )
            idx = css.find(token, idx + 1)


def test_step_report_rules_stay_out_of_the_desktop_cascade():
    """The report-card hardening is breakpoint-local — desktop stays unchanged.

    Every new selector is `#flow-view .flow-conversation` / `#history-view
    .history-detail` prefixed AND lives inside a media query, so no wide-viewport
    rendering can match it.
    """
    css = STYLE_CSS.read_text(encoding="utf-8")
    ranges = _media_ranges(css)
    for token in (
        "#flow-view .flow-conversation .conv-record",
        "#flow-view .flow-conversation .step-report,",
        "#flow-view .flow-conversation .step-report__kv-k",
        "#flow-view .flow-conversation {",
        "#history-view .history-detail .step-report,",
        "#history-view .history-detail .history-step-header",
    ):
        idx = css.find(token)
        assert idx != -1, f"expected {token!r} in style.css"
        while idx != -1:
            assert _inside_media(idx, ranges), (
                f"{token!r} at offset {idx} is OUTSIDE a media query — it would "
                f"change the desktop console"
            )
            idx = css.find(token, idx + 1)

    # The desktop `.step-report__kv-k` alignment floor must survive untouched:
    # only the mobile overlay relaxes it.
    start = css.find("\n.step-report__kv-k {")
    assert start != -1, "desktop .step-report__kv-k rule is missing"
    desktop_rule = css[start : css.find("}", start)]
    assert "min-width: 100px" in desktop_rule, (
        "the desktop kv key column must keep its 100px alignment floor"
    )


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
        ".flow-conversation .tool-marker {",
        "#flow-view .flow-reply-context.active",
        "max-height: 35vh",
        # History mobile overflow hardening — every new #history-view selector
        # must stay inside the breakpoint.
        "#history-view .history-list-pane",
        "#history-view .history-detail-pane",
        "#history-view .history-project-select",
        "#history-view .history-step-title",
        "#history-view .msg-chip",
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
