/*
 * Mobile-responsive pure-helper tests (Group G7).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerMobileResponsiveTests({app, check, findOne, findAll})` so the parent
 * harness drives the same check() reporter and the same `app` module export.
 *
 * The mobile-portrait pass (G1–G6) is overwhelmingly CSS that only takes effect
 * inside the @media (max-width: 600px) breakpoint; the only JavaScript it adds
 * are a handful of DOM-free state-transition helpers that flip a class
 * (hamburger open/close, main-list machines↔flows panel switch, History
 * list↔detail switch, flow-view sidebar drawer open/close). Those helpers are
 * the testable surface here — pure functions with no browser dependency, so we
 * assert their state machines directly. The visual rules themselves are covered
 * by the static-source guards in tests/test_frontend_mobile.py.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

// Resolve and read style.css once so the G4 step-grouping / mobile-grouping
// static-source guards can assert the key rules exist and live at the correct
// scope (desktop grouping at top level, mobile containment inside the
// @media (max-width: 600px) breakpoint).
const HERE = path.dirname(fileURLToPath(import.meta.url));
const STYLE_CSS = path.join(
  HERE, "..", "..", "src", "tianluo", "server", "static", "style.css");
const CSS = fs.readFileSync(STYLE_CSS, "utf8");

// Brace-balanced body of the @media (max-width: 600px) block (mirrors the
// Python guard's _block_text). Returns { start, end } char offsets of the
// block body so callers can test in/out-of-breakpoint scope.
function mobileBreakpointRange(css) {
  const open = "@media (max-width: 600px) {";
  const start = css.indexOf(open);
  assert.notEqual(start, -1, "missing @media (max-width: 600px) block");
  const brace = css.indexOf("{", start);
  let depth = 0;
  let j = brace;
  for (; j < css.length; j++) {
    if (css[j] === "{") depth += 1;
    else if (css[j] === "}") {
      depth -= 1;
      if (depth === 0) break;
    }
  }
  return { start: brace, end: j };
}

export function registerMobileResponsiveTests(ctx) {
  const { app, check } = ctx;

  const MOBILE = mobileBreakpointRange(CSS);
  const insideMobile = (idx) => idx > MOBILE.start && idx < MOBILE.end;

  // -- navMenuNextState (G2 topbar overflow menu) ---------------------------
  // The hamburger is a plain toggle: the next state is simply the negation of
  // the current open flag. Two toggles return to the start (involution).
  check("G7 navMenuNextState toggles the open flag", () => {
    assert.equal(app.navMenuNextState(false), true);
    assert.equal(app.navMenuNextState(true), false);
  });
  check("G7 navMenuNextState is an involution (toggle twice → start)", () => {
    assert.equal(app.navMenuNextState(app.navMenuNextState(false)), false);
    assert.equal(app.navMenuNextState(app.navMenuNextState(true)), true);
  });
  check("G7 navMenuNextState coerces truthy/falsy inputs to a boolean", () => {
    assert.equal(app.navMenuNextState(undefined), true);
    assert.equal(app.navMenuNextState(null), true);
    assert.equal(app.navMenuNextState(0), true);
    assert.equal(app.navMenuNextState(1), false);
    assert.equal(app.navMenuNextState("open"), false);
  });

  // -- flowSidebarNextState (G4 running-console off-canvas drawer) -----------
  // Mirrors navMenuNextState: the sidebar drawer is a plain open/close toggle.
  check("G7 flowSidebarNextState toggles the open flag", () => {
    assert.equal(app.flowSidebarNextState(false), true);
    assert.equal(app.flowSidebarNextState(true), false);
  });
  check("G7 flowSidebarNextState is an involution", () => {
    assert.equal(app.flowSidebarNextState(app.flowSidebarNextState(true)), true);
  });
  check("G7 flowSidebarNextState coerces non-boolean inputs", () => {
    assert.equal(app.flowSidebarNextState(undefined), true);
    assert.equal(app.flowSidebarNextState("x"), false);
  });

  // -- listPanelState (G3 main-list Machines↔Flows panel switch) ------------
  // The main list collapses to a single visible panel on a phone. The state is
  // "machines" (default) or "flows"; selecting a machine reveals flows, the
  // back button / a reset returns to machines, and an unknown action is inert
  // (the current panel is preserved, defaulting to "machines").
  check("G7 listPanelState: select-machine → flows", () => {
    assert.equal(app.listPanelState("machines", "select-machine"), "flows");
    assert.equal(app.listPanelState("flows", "select-machine"), "flows");
  });
  check("G7 listPanelState: back / reset → machines", () => {
    assert.equal(app.listPanelState("flows", "back"), "machines");
    assert.equal(app.listPanelState("flows", "reset"), "machines");
    assert.equal(app.listPanelState("machines", "reset"), "machines");
  });
  check("G7 listPanelState: unknown action preserves the current panel", () => {
    assert.equal(app.listPanelState("flows", "noop"), "flows");
    assert.equal(app.listPanelState("machines", "noop"), "machines");
    // A bogus current value normalizes to the safe "machines" default.
    assert.equal(app.listPanelState("garbage", "noop"), "machines");
    assert.equal(app.listPanelState(undefined, undefined), "machines");
  });
  check("G7 listPanelState: full select→back round trip returns to machines", () => {
    let panel = "machines";
    panel = app.listPanelState(panel, "select-machine");
    assert.equal(panel, "flows");
    panel = app.listPanelState(panel, "back");
    assert.equal(panel, "machines");
  });

  // -- historyPanelState (G5 History list↔detail panel switch) --------------
  // Isomorphic to the main-list switch but over list/detail: selecting a
  // session reveals the detail, back / reset return to the list.
  check("G7 historyPanelState: select-session → detail", () => {
    assert.equal(app.historyPanelState("list", "select-session"), "detail");
    assert.equal(app.historyPanelState("detail", "select-session"), "detail");
  });
  check("G7 historyPanelState: back / reset → list", () => {
    assert.equal(app.historyPanelState("detail", "back"), "list");
    assert.equal(app.historyPanelState("detail", "reset"), "list");
    assert.equal(app.historyPanelState("list", "reset"), "list");
  });
  check("G7 historyPanelState: unknown action preserves the current panel", () => {
    assert.equal(app.historyPanelState("detail", "noop"), "detail");
    assert.equal(app.historyPanelState("list", "noop"), "list");
    assert.equal(app.historyPanelState("garbage", "noop"), "list");
    assert.equal(app.historyPanelState(undefined, undefined), "list");
  });
  check("G7 historyPanelState: full select→back round trip returns to list", () => {
    let panel = "list";
    panel = app.historyPanelState(panel, "select-session");
    assert.equal(panel, "detail");
    panel = app.historyPanelState(panel, "back");
    assert.equal(panel, "list");
  });

  // -- replyTextareaHeight (G3 WeChat-style auto-grow clamp) ----------------
  // Pure clamp: the applied height is the content scrollHeight clamped into
  // [minPx, maxPx]. below-min → minPx, in-range → scrollHeight, above-max →
  // maxPx; non-finite / out-of-order inputs degrade deterministically.
  check("G7 replyTextareaHeight clamps below-min up to minPx", () => {
    assert.equal(app.replyTextareaHeight(20, 40, 200), 40);
    assert.equal(app.replyTextareaHeight(40, 40, 200), 40);
  });
  check("G7 replyTextareaHeight returns scrollHeight when in range", () => {
    assert.equal(app.replyTextareaHeight(100, 40, 200), 100);
    assert.equal(app.replyTextareaHeight(199, 40, 200), 199);
  });
  check("G7 replyTextareaHeight clamps above-max down to maxPx", () => {
    assert.equal(app.replyTextareaHeight(500, 40, 200), 200);
    assert.equal(app.replyTextareaHeight(200, 40, 200), 200);
  });
  check("G7 replyTextareaHeight floors fractional pixels", () => {
    assert.equal(app.replyTextareaHeight(100.9, 40.2, 200.7), 100);
  });
  check("G7 replyTextareaHeight collapses an empty/default field to one line", () => {
    // Problem 4: the auto-grow measures scrollHeight after pinning the textarea
    // height to 0, so an empty / default field reports a tiny content height
    // (≈ one line + padding, and in the degenerate stub case 0). That small
    // measurement must clamp UP to the single-line minimum (40px), never the
    // old ~6-row height — proving the default state collapses to one row.
    assert.equal(app.replyTextareaHeight(0, 40, 300), 40);
    assert.equal(app.replyTextareaHeight(38, 40, 300), 40);
    // The first content line past the floor grows past it.
    assert.equal(app.replyTextareaHeight(64, 40, 300), 64);
  });
  check("G7 replyTextareaHeight degrades on non-finite / illegal input", () => {
    // Bad scrollHeight → fall back to the minimum, never NaN.
    assert.equal(app.replyTextareaHeight(NaN, 40, 200), 40);
    assert.equal(app.replyTextareaHeight(undefined, 40, 200), 40);
    // Bad min → treated as 0 floor; content still honored / capped.
    assert.equal(app.replyTextareaHeight(100, NaN, 200), 100);
    assert.equal(app.replyTextareaHeight(300, NaN, 200), 200);
    // Bad max → collapses to min so the result never exceeds the floor.
    assert.equal(app.replyTextareaHeight(300, 40, NaN), 40);
    // Out-of-order bounds (max < min) → max raised to min.
    assert.equal(app.replyTextareaHeight(300, 80, 40), 80);
    const h = app.replyTextareaHeight("garbage", "x", "y");
    assert.ok(Number.isFinite(h), "result is always a finite number");
  });

  // -- cross-helper invariant: the two panel switches never cross-leak -------
  // list uses machines/flows, history uses list/detail; their vocabularies are
  // disjoint so a stray action on one never yields the other's panel name.
  check("G7 panel switches keep disjoint vocabularies", () => {
    const listVals = new Set();
    const histVals = new Set();
    for (const cur of ["machines", "flows", "list", "detail", "x"]) {
      for (const act of ["select-machine", "select-session", "back", "reset", "noop"]) {
        listVals.add(app.listPanelState(cur, act));
        histVals.add(app.historyPanelState(cur, act));
      }
    }
    assert.deepEqual([...listVals].sort(), ["flows", "machines"]);
    assert.deepEqual([...histVals].sort(), ["detail", "list"]);
  });

  // -- History detail: long-content rendering routes into CSS-guarded classes --
  //
  // The CSS guard (test_frontend_mobile.py) locks wrapping rules onto selectors
  // like `.conv-bubble`, `.msg-chip`, `.raw-json`, and `.step-report__list li`.
  // This DOM-level regression verifies that renderConversation actually routes
  // long content into elements carrying those classes — if the rendering path
  // changes to use different class names, the CSS guard becomes useless.
  //
  // We exercise four long-content shapes:
  //   (1) a 200+ character no-space string (the unbreakable run that must wrap)
  //   (2) a long file path (typical step output)
  //   (3) a raw_json payload (the "view raw" toggle)
  //   (4) a step_completed event whose outputs carry a long list item

  // Recursive class search (mirrors test_app_pure.mjs findAll).
  function findAllG7(node, cls, acc) {
    if (!acc) acc = [];
    if (node.classList && node.classList.contains(cls)) acc.push(node);
    if (node.childNodes) {
      for (const c of node.childNodes) findAllG7(c, cls, acc);
    }
    return acc;
  }
  function findOneG7(node, cls) { return findAllG7(node, cls)[0] || null; }

  const LONG_NO_SPACE = "a".repeat(220);
  const LONG_PATH = "/very/deep/nested/project/src/components/feature/tabs/AdvancedSettingsPanel.integration.test.tsx";

  check("G7 history detail: long assistant content routes into .conv-bubble", () => {
    // An assistant record with a 220-char no-space string. renderConversation
    // must wrap it inside a .conv-bubble node.
    const container = document.createElement("div");
    app.renderConversation(container, [{
      step_id: "s1",
      step_type: "discovery",
      message: { role: "assistant", content: LONG_NO_SPACE, timestamp: 1 },
    }], false);
    const bubble = findOneG7(container, "conv-bubble");
    assert.ok(bubble, "assistant content must render inside a .conv-bubble node");
    assert.ok(bubble.textContent.includes(LONG_NO_SPACE.slice(0, 40)),
      "the long no-space string must be present in the bubble text");
  });

  check("G7 history detail: long log content routes into .conv-bubble .foldable", () => {
    // A long log record (>FOLD_THRESHOLD=1600) renders as a non-collapsible
    // "other" role row with a .conv-bubble containing a .foldable wrapper.
    // The foldable's toggle and detail carry the long content.
    const container = document.createElement("div");
    const longLog = "x".repeat(2500);
    app.renderConversation(container, [{
      step_id: "s1",
      step_type: "discovery",
      message: { role: "log", content: longLog, timestamp: 1 },
    }], false);
    const bubble = findOneG7(container, "conv-bubble");
    assert.ok(bubble, "log content must render inside a .conv-bubble node");
    const foldable = findOneG7(bubble, "foldable");
    assert.ok(foldable, "a long log record (>FOLD_THRESHOLD) must render as .foldable");
    // The foldable contains a toggle and the full text.
    const toggle = findOneG7(foldable, "fold-toggle");
    assert.ok(toggle, "the foldable must carry a .fold-toggle button");
    assert.ok(foldable.textContent.length > 100,
      "the foldable must contain the long body text");
  });

  check("G7 history detail: raw_json payload produces .raw-toggle + .raw-json", () => {
    // A record with raw_json payload: the "view raw" toggle is always present
    // on every conversation message (unified view-raw principle). The toggle
    // wrapper carries the .raw-toggle class and contains a .raw-json pre
    // (CSS-guarded for wrapping). makeAssistantRawToggle creates the structure
    // raw-toggle-wrap > button.raw-toggle + pre.raw-json.
    const container = document.createElement("div");
    app.renderConversation(container, [{
      step_id: "s1",
      step_type: "discovery",
      message: {
        role: "assistant",
        content: "short",
        raw_json: [{ type: "assistant", message: { content: "short" } }],
      },
      timestamp: 1,
    }], false);
    // Every assistant message must have a view-raw affordance (makeAssistantRawToggle).
    const rawBtn = findOneG7(container, "raw-toggle");
    assert.ok(rawBtn, "assistant message with raw_json must carry a .raw-toggle button");
    // The .raw-json <pre> node sits beside the button inside the wrap.
    const rawJson = findOneG7(container, "raw-json");
    assert.ok(rawJson, "the raw toggle must produce a .raw-json node (CSS guard target)");
  });

  check("G7 history detail: step_completed long list item routes into .step-report__list li", () => {
    // A step_completed event whose outputs include a tests_added list with a
    // long path. The report renderer uses reportList() which produces
    // .step-report__list > li nodes. The CSS guard locks wrapping onto those li.
    const container = document.createElement("div");
    app.renderConversation(container, [{
      step_id: "s1",
      step_type: "test",
      message: {
        type: "step_completed",
        timestamp: 1,
        data: {
          step_type: "test",
          outputs: {
            tests_added: [LONG_PATH, "short_test.py"],
            overall_status: "PASSED",
          },
        },
      },
    }], false);
    const reportList = findOneG7(container, "step-report__list");
    if (reportList) {
      const items = reportList.children.filter((c) => c.tagName === "LI");
      assert.ok(items.length >= 1, "report list must have at least one li item");
      const hasLong = items.some((li) => li.textContent.includes(LONG_PATH));
      assert.ok(hasLong, "the long path must appear in a report list li");
    }
    // Also verify the .step-report container exists (the card is rendered).
    const report = findOneG7(container, "step-report");
    assert.ok(report, "step_completed must render a .step-report card");
  });

  check("G7 history detail: assistant bubble with long path carries content", () => {
    // An assistant turn whose body contains a long file path — the path must
    // be present in the .conv-bubble text (not dropped or truncated to a
    // different DOM node).
    const container = document.createElement("div");
    app.renderConversation(container, [{
      step_id: "s1",
      step_type: "analyze",
      message: {
        role: "assistant",
        content: `Analysis complete. Modified file: ${LONG_PATH}\n\nDone.`,
        timestamp: 1,
      },
    }], false);
    const bubble = findOneG7(container, "conv-bubble");
    assert.ok(bubble, "assistant content must render inside .conv-bubble");
    assert.ok(bubble.textContent.includes(LONG_PATH),
      "the long path must appear in the bubble, not be dropped");
  });

  // -- G4 step-grouping style guards ----------------------------------------
  //
  // G4 turns the `step-type-<type>` DOM class (added by addConversationRecords,
  // G2) into a stable, low-saturation, distinguishable per-step grouping style.
  // These static-source guards (mirroring the Python style guard's approach,
  // but kept in the listed G4 test file) lock that the key grouping classes and
  // their mobile-containment rules exist and live at the correct scope:
  //   - desktop grouping rules at the TOP LEVEL (so both running + history
  //     views, which share `.history-detail`, group identically);
  //   - mobile containment rules strictly INSIDE the 600px breakpoint (so the
  //     desktop layout stays byte-for-byte unchanged).

  // Active step types that must carry a distinguishable grouping accent.
  const G4_STEP_TYPES = [
    "discovery", "analyze", "plan", "implement", "test", "self_check",
    "verify_spec", "update_spec", "spec_gate", "version_analyze", "commit",
    "summarize",
  ];

  check("G4 :root defines a per-step grouping accent var for every step type", () => {
    for (const t of G4_STEP_TYPES) {
      assert.ok(CSS.includes(`--step-${t}:`),
        `:root must define the --step-${t} grouping accent variable`);
    }
  });

  check("G4 each step type has a top-level .history-detail grouping rule", () => {
    for (const t of G4_STEP_TYPES) {
      const sel = `.history-detail .conv-record.step-type-${t}`;
      const idx = CSS.indexOf(sel);
      assert.notEqual(idx, -1, `missing grouping rule for ${sel}`);
      // Must be a DESKTOP (top-level) rule so running + history views share it.
      assert.ok(!insideMobile(idx),
        `${sel} must live at the top level (shared by both views), not inside the breakpoint`);
      // Rule body must recolour the left rail to the step accent and tint the
      // lane. The faint lane colour now rides the --step-lane custom property
      // (rendered through the continuous ::before underlay — see the G2
      // step_lane_continuity tests) rather than a per-record `background`.
      const body = CSS.slice(idx, CSS.indexOf("}", idx));
      assert.ok(body.includes(`border-left-color: var(--step-${t})`),
        `${sel} must set border-left-color to var(--step-${t})`);
      assert.ok(/--step-lane:\s*rgba\(/.test(body),
        `${sel} must set a faint rgba lane colour via --step-lane`);
    }
  });

  check("G4 grouping rules exclude the lightweight status / DAG markers", () => {
    // The status / group-status markers carry a step-type class too, so the
    // grouping rules must NOT clobber their own status colouring.
    const sel = ".history-detail .conv-record.step-type-test";
    const idx = CSS.indexOf(sel);
    const body = CSS.slice(idx, CSS.indexOf("{", idx));
    assert.ok(body.includes(":not(.step-status-row)")
      && body.includes(":not(.group-status-marker)"),
      "grouping rule must exclude .step-status-row and .group-status-marker");
  });

  check("G4 report card border colours match the per-step grouping accents", () => {
    // The report card (the step's "result/summary") reads in the same colour
    // as its step region.
    for (const t of ["discovery", "implement", "test", "commit", "summarize"]) {
      const sel = `.step-report.kind-${t}`;
      const idx = CSS.indexOf(sel);
      assert.notEqual(idx, -1, `missing report-card colour rule for ${sel}`);
      const body = CSS.slice(idx, CSS.indexOf("}", idx));
      assert.ok(body.includes(`border-left-color: var(--step-${t})`),
        `${sel} must use the matching var(--step-${t}) accent`);
    }
  });

  check("G4 mobile containment rules live inside the 600px breakpoint and scope to flow/history views", () => {
    // The mobile grouping containment must be scoped to #flow-view / #history-view
    // and sit strictly inside the breakpoint so desktop is unaffected.
    const tokens = [
      "#flow-view .flow-conversation .conv-record",
      "#history-view .history-detail .conv-record",
      "#flow-view .step-status-row",
      "#history-view .step-status-row",
    ];
    for (const tok of tokens) {
      const idx = CSS.indexOf(tok);
      assert.notEqual(idx, -1, `missing mobile containment token ${tok}`);
      assert.ok(insideMobile(idx),
        `${tok} must live INSIDE the @media (max-width: 600px) breakpoint`);
    }
  });

  check("G4 mobile status row wraps long labels and never adds horizontal scroll", () => {
    const sel = "#flow-view .step-status-row .step-status-text";
    const idx = CSS.indexOf(sel);
    assert.notEqual(idx, -1, `missing mobile status-text wrap rule ${sel}`);
    const body = CSS.slice(idx, CSS.indexOf("}", idx));
    assert.ok(
      body.includes("overflow-wrap: anywhere") || body.includes("word-break: break-word"),
      `${sel} must carry a per-character break rule`);
    // The status row itself must shrink (min-width: 0) and wrap, not scroll.
    const rowIdx = CSS.indexOf("#flow-view .step-status-row,");
    const rowBody = CSS.slice(rowIdx, CSS.indexOf("}", rowIdx));
    assert.ok(rowBody.includes("flex-wrap: wrap") && rowBody.includes("min-width: 0"),
      "the mobile status row must wrap (flex-wrap) and shrink (min-width: 0)");
    // No horizontal-scroll escape hatch anywhere in the G4 mobile additions.
    const groupBlock = CSS.slice(
      CSS.indexOf("per-step grouping: contain on mobile"),
      CSS.indexOf("idle reply placeholder: shrink"));
    assert.ok(!groupBlock.includes("overflow-x: auto"),
      "G4 mobile grouping rules must NOT use overflow-x: auto");
  });

  check("G4 DOM: records of different step types carry distinct step-type classes", () => {
    // The grouping CSS keys off `step-type-<type>` on each record. Verify the
    // render path actually applies a per-step-type class so the styling target
    // exists for both the flow and history views (shared renderConversation).
    const container = document.createElement("div");
    app.renderConversation(container, [
      {
        step_id: "01_analyze_aa", step_type: "analyze",
        message: { role: "assistant", content: "analysis", timestamp: 1 },
      },
      {
        step_id: "02_implement_bb", step_type: "implement",
        message: { role: "assistant", content: "impl", timestamp: 2 },
      },
    ], false);
    assert.ok(findOneG7(container, "step-type-analyze"),
      "an analyze record must carry the step-type-analyze class");
    assert.ok(findOneG7(container, "step-type-implement"),
      "an implement record must carry the step-type-implement class");
  });

  // -- step-result report cards: mobile overflow hardening ------------------
  //
  // The report cards (12.12.0) postdate the mobile pass, so every
  // `.step-report__*` rule was authored at the top level with no wrapping
  // declaration, and none of them appeared inside the 600px breakpoint. A
  // no-space token in a rendered field (a real analyze `outputs.scope` in this
  // repo's archive carries an 83-character run) then blew the column open, and
  // because `.flow-conversation` is its own scroll container (`overflow-y: auto`
  // forces computed `overflow-x: auto`) the user could swipe the running console
  // sideways — the mobile `html, body { overflow-x: hidden }` backstop never saw
  // it. These guards lock the three layers of the fix at the correct scope; the
  // real geometry (scrollWidth === clientWidth at 390px) is asserted by the
  // Chromium path in tests/test_frontend_mobile_overflow.py.

  check("G7 conversation wrapping rule lives inside the 600px breakpoint", () => {
    const sel = "#flow-view .flow-conversation .conv-record,\n"
      + "  #flow-view .flow-conversation .history-step-header,";
    const idx = CSS.indexOf(sel);
    assert.notEqual(idx, -1, "missing the conversation wrapping rule");
    assert.ok(insideMobile(idx),
      "the conversation wrapping rule must live INSIDE @media (max-width: 600px)");
    const body = CSS.slice(idx, CSS.indexOf("}", idx));
    // overflow-wrap/word-break INHERIT, so declaring them on the record reaches
    // every step-report construct, markdown node and anonymous inline box.
    assert.ok(body.includes("overflow-wrap: anywhere"),
      "must use `anywhere` — `break-word` alone does not reduce the min-content "
      + "contribution, so a flex item such as .step-report__stat stays wider "
      + "than the column");
    assert.ok(body.includes("word-break: break-word"),
      "keep word-break: break-word for legacy-WebKit parity");
    // Both views that share the renderers must be covered.
    for (const view of ["#flow-view .flow-conversation", "#history-view .history-detail"]) {
      assert.ok(body.includes(`${view} .conv-record`),
        `${view} .conv-record must share the wrapping rule`);
    }
  });

  check("G7 report-card flex chain carries shrink protection inside the breakpoint", () => {
    const sel = "#flow-view .flow-conversation .step-report,";
    const idx = CSS.indexOf(sel);
    assert.notEqual(idx, -1, `missing report-card shrink rule ${sel}`);
    assert.ok(insideMobile(idx), "the shrink rule must live inside the breakpoint");
    const body = CSS.slice(idx, CSS.indexOf("}", idx));
    for (const construct of [
      "step-report__head", "step-report__body", "step-report__status-bar",
      "step-report__section", "step-report__list", "step-report__kv-row",
      "step-report__kv-nested", "step-report__conv-turn", "step-report__markdown",
    ]) {
      for (const view of ["#flow-view .flow-conversation", "#history-view .history-detail"]) {
        assert.ok(body.includes(`${view} .${construct}`),
          `${view} .${construct} must be in the shrink-protection rule`);
      }
    }
    assert.ok(body.includes("min-width: 0") && body.includes("max-width: 100%"),
      "shrink protection is min-width: 0 + max-width: 100%");
  });

  check("G7 the conversation scroll container cannot scroll horizontally on mobile", () => {
    const sel = "#flow-view .flow-conversation {";
    const idx = CSS.indexOf(sel);
    assert.notEqual(idx, -1, `missing ${sel} containment rule`);
    assert.ok(insideMobile(idx), "the containment rule must live inside the breakpoint");
    const body = CSS.slice(idx, CSS.indexOf("}", idx));
    assert.ok(body.includes("overflow-x: hidden"),
      ".flow-conversation must pin overflow-x: hidden — its overflow-y: auto "
      + "otherwise computes overflow-x to auto and the console becomes swipeable");
  });

  check("G7 constructs that override the inherited wrapping are released too", () => {
    // The sweep works by inheritance, which reaches a construct only while it
    // does not set the competing property itself. `.agent-badge` pins
    // `white-space: nowrap` and `.tool-marker-name` / `.tool-marker-input-key`
    // are `flex-shrink: 0`, so a long agent/model id or a structured
    // `mcp__<server>__<tool>` name stays one unbreakable box wider than the
    // column — containment would only hide it, not wrap it.
    const badgeIdx = CSS.indexOf("#flow-view .flow-conversation .agent-badge,");
    assert.notEqual(badgeIdx, -1, "missing the mobile agent-badge release");
    assert.ok(insideMobile(badgeIdx), "the badge release must live inside the breakpoint");
    const badge = CSS.slice(badgeIdx, CSS.indexOf("}", badgeIdx));
    assert.ok(badge.includes("#history-view .history-detail .agent-badge"),
      "the History view renders the same badge and must share the release");
    assert.ok(badge.includes("white-space: normal"),
      "the badge's desktop nowrap must be released, or the inherited "
      + "overflow-wrap can never take effect");
    assert.ok(badge.includes("overflow-wrap: anywhere") && badge.includes("max-width: 100%"));

    const chipIdx = CSS.indexOf("#flow-view .flow-conversation .tool-marker-name,");
    assert.notEqual(chipIdx, -1, "missing the mobile tool-chip release");
    assert.ok(insideMobile(chipIdx), "the chip release must live inside the breakpoint");
    const chip = CSS.slice(chipIdx, CSS.indexOf("}", chipIdx));
    for (const construct of ["tool-marker-name", "tool-marker-input-key",
                             "tool-marker-glyph", "tool-marker-toggle"]) {
      for (const view of ["#flow-view .flow-conversation", "#history-view .history-detail"]) {
        assert.ok(chip.includes(`${view} .${construct}`),
          `${view} .${construct} must be released on mobile`);
      }
    }
    assert.ok(chip.includes("flex-shrink: 1") && chip.includes("min-width: 0")
      && chip.includes("overflow-wrap: anywhere"));

    // Desktop keeps the one-line pill and the rigid chip-head columns.
    const dBadge = CSS.indexOf("\n.agent-badge {");
    assert.ok(!insideMobile(dBadge + 1));
    assert.ok(CSS.slice(dBadge, CSS.indexOf("}", dBadge)).includes("white-space: nowrap"));
    const dName = CSS.indexOf("\n.tool-marker-name {");
    assert.ok(!insideMobile(dName + 1));
    assert.ok(CSS.slice(dName, CSS.indexOf("}", dName)).includes("flex-shrink: 0"));
  });

  check("G7 the nested-kv indent ladder is bounded inside the breakpoint", () => {
    // `renderGenericKvRow` recurses over `step.outputs` with no depth limit and
    // `.step-report__kv-nested` is a `flex-basis: 100%` item, so every level's
    // 16px margin + 8px padding + 1px border SUBTRACTS from the usable column.
    // At 320px that ladder exhausts the column around the twelfth level, and
    // wrapping cannot rescue a column with no width left. Mobile therefore
    // narrows one level AND stops the ladder accruing past the fourth.
    const narrow = "#flow-view .flow-conversation .step-report__kv-nested,\n"
      + "  #history-view .history-detail .step-report__kv-nested {";
    const nIdx = CSS.indexOf(narrow);
    assert.notEqual(nIdx, -1, "missing the mobile nested-kv indent rule");
    assert.ok(insideMobile(nIdx), "the indent rule must live inside the breakpoint");
    const nBody = CSS.slice(nIdx, CSS.indexOf("}", nIdx));
    assert.ok(nBody.includes("margin-left: 8px") && nBody.includes("padding-left: 6px"),
      "one nested level must be narrower on a phone than the desktop ladder");

    const UNIT = ".step-report__kv-nested";
    const capHead = `#flow-view .flow-conversation ${UNIT} ${UNIT}`;
    const cIdx = CSS.indexOf(capHead);
    assert.notEqual(cIdx, -1,
      `missing the nested-kv depth cap — a selector chaining ${UNIT} onto `
      + "itself, without which indentation grows with the data");
    assert.ok(insideMobile(cIdx), "the depth cap must live inside the breakpoint");
    const cBody = CSS.slice(cIdx, CSS.indexOf("}", cIdx));
    assert.ok(cBody.includes(`#history-view .history-detail ${UNIT} ${UNIT}`),
      "the History view renders the same card and must share the depth cap");
    // Five chained units per view (four ancestors + the matched element), so
    // the cap engages at the fifth level and every level below it.
    assert.ok(cBody.split(UNIT).length - 1 >= 10,
      "the cap must chain five " + UNIT + " per view across both views");
    for (const decl of ["margin-left: 0", "padding-left: 0", "border-left: none"]) {
      assert.ok(cBody.includes(decl), `the depth-cap rule must reset ${decl}`);
    }

    // Desktop keeps the full ladder — this is a breakpoint overlay only.
    const base = CSS.indexOf("\n.step-report__kv-nested {");
    assert.ok(!insideMobile(base + 1), "the base ladder must stay outside the breakpoints");
    const bBody = CSS.slice(base, CSS.indexOf("}", base));
    assert.ok(bBody.includes("margin-left: 16px") && bBody.includes("padding-left: 8px"),
      "desktop indentation must be unchanged by this pass");
  });

  check("G7 DOM: deeply nested generic outputs stay one nesting chain", () => {
    // The CSS cap is expressed as a self-descendant chain, so it only works
    // while app.js keeps nesting `.step-report__kv-nested` inside itself for
    // each level. Assert the recursion still produces that shape (and still has
    // no depth limit of its own, which is what makes the CSS cap load-bearing).
    const LEVELS = 14;
    let value = {leaf: "a".repeat(120)};
    for (let i = LEVELS; i >= 1; i--) value = {[`level_${i}`]: value};
    const container = document.createElement("div");
    app.renderConversation(container, [{
      step_id: "01_generic_probe_aa",
      step_type: "generic_probe",
      message: {
        type: "step_completed", timestamp: 1,
        data: {step_type: "generic_probe", status: "completed", outputs: {deep: value}},
      },
    }], false);
    // Each level contributes exactly one wrapper, nested inside the previous
    // one — which is precisely the shape the CSS self-descendant cap matches.
    const wrappers = findAllG7(container, "step-report__kv-nested");
    assert.equal(wrappers.length, LEVELS + 1,
      `renderGenericKvRow must recurse once per level (got ${wrappers.length} `
      + `wrappers for ${LEVELS} nested dicts + the leaf holder); if it ever `
      + "gains its own depth limit, revisit the CSS indent cap");
    const ancestorDepth = (node) => {
      let d = 0;
      for (let p = node.parentNode; p; p = p.parentNode) {
        if (p.classList && p.classList.contains("step-report__kv-nested")) d += 1;
      }
      return d;
    };
    const depths = wrappers.map(ancestorDepth).sort((a, b) => a - b);
    assert.deepEqual(depths, wrappers.map((_, i) => i),
      "the wrappers must form ONE chain, each inside the previous — the mobile "
      + "indent cap is a self-descendant selector and only bounds that shape");
  });

  check("G7 DOM: an assistant turn renders the badge and a structured tool chip", () => {
    // The CSS release only matters while app.js still emits these constructs
    // inside the conversation. `raw_json` drives the rich chip path, which —
    // unlike the legacy bracket parser — renders ANY tool name, so an MCP tool
    // name reaches `.tool-marker-name` verbatim.
    const container = document.createElement("div");
    const TOOL = "mcp__tianluo_flow_control_plane__inspect_running_flow_conversation";
    app.renderConversation(container, [{
      step_id: "01_implement_aa",
      step_type: "implement",
      message: {
        role: "assistant", timestamp: 1,
        agent_name: "oclaude-worktree-implementer-with-a-very-long-configured-name",
        model_name: "claude-opus-5-20260514-extended-thinking[1m]",
        content: `Calling [Tool: ${TOOL}] now.`,
        raw_json: [
          {type: "text", text: `Calling [Tool: ${TOOL}] now.`},
          {type: "tool_use", id: "tu_1", name: TOOL, input: {path: "src/app.js"}},
        ],
      },
    }], false);
    const badge = findOneG7(container, "agent-badge");
    assert.ok(badge, "the agent/model badge must render inside the conversation");
    assert.ok(badge.textContent.includes("·"),
      "the badge shows `agent · model`, which is the long-token case");
    const name = findOneG7(container, "tool-marker-name");
    assert.ok(name, "the structured chip must render a .tool-marker-name");
    assert.equal(name.textContent, TOOL,
      "the full mcp__server__tool name lands in .tool-marker-name — a single "
      + "unbreakable token, which is why it needs the mobile release");
  });

  check("G7 the desktop step-report rules keep no mobile wrapping", () => {
    // Every top-level `.step-report*` rule must stay free of the mobile-only
    // wrapping/containment, or desktop layout would change.
    for (const sel of [".step-report {", ".step-report__status-bar {", ".step-report__head {"]) {
      const idx = CSS.indexOf(sel);
      assert.notEqual(idx, -1, `missing desktop rule ${sel}`);
      assert.ok(!insideMobile(idx), `${sel} must remain a top-level desktop rule`);
      const body = CSS.slice(idx, CSS.indexOf("}", idx));
      assert.ok(!body.includes("overflow-wrap: anywhere"),
        `${sel} must not gain mobile wrapping at the top level`);
    }
    // The kv key column keeps its desktop alignment floor; only mobile relaxes it.
    const kvIdx = CSS.indexOf(".step-report__kv-k {");
    assert.ok(!insideMobile(kvIdx));
    assert.ok(CSS.slice(kvIdx, CSS.indexOf("}", kvIdx)).includes("min-width: 100px"),
      "the desktop kv key column must keep min-width: 100px");
  });

  check("G7 DOM: an analyze step_completed routes its scope into .step-report__stat", () => {
    // The CSS fix reaches the scope row only because it is rendered inside the
    // report card. If renderAnalyzeReport stops emitting the scope as a
    // .step-report__stat inside a .conv-record, the inherited wrapping no longer
    // applies and this guard fails before the visual regression ships.
    const container = document.createElement("div");
    const SCOPE = "src/tianluo/server/static/app.js " + "a".repeat(120);
    app.renderConversation(container, [{
      step_id: "01_analyze_aa",
      step_type: "analyze",
      message: {
        type: "step_completed",
        timestamp: 1,
        data: {step_type: "analyze", status: "completed",
               outputs: {task_type: "bugfix", complexity: "medium", scope: SCOPE}},
      },
    }], false);
    const record = findOneG7(container, "conv-record");
    assert.ok(record, "the step event must render inside a .conv-record");
    const stats = findAllG7(record, "step-report__stat");
    assert.ok(stats.length >= 3, "analyze renders task / complexity / scope stats");
    const scopeStat = stats.find((s) => s.textContent.includes(SCOPE));
    assert.ok(scopeStat,
      "the whole scope string must land in a single .step-report__stat inside "
      + "the record, which is what the inherited wrapping rule targets");
  });

  check("G7 DOM: the step-report card is a .conv-record, not a .history-step child", () => {
    // The fix relies on inheritance from .conv-record. No `.history-step`
    // wrapper is ever built for the conversation (only .history-step-header
    // separators), so nothing else would clip or wrap the card.
    const container = document.createElement("div");
    app.renderConversation(container, [{
      step_id: "01_test_aa",
      step_type: "test",
      message: {type: "step_completed", timestamp: 1,
                data: {step_type: "test", outputs: {overall_status: "PASSED"}}},
    }], false);
    assert.ok(findOneG7(container, "step-report"), "the report card must render");
    assert.equal(findAllG7(container, "history-step").length, 0,
      "no .history-step wrapper is built for the conversation — the card must "
      + "own its mobile overflow hardening rather than inherit containment");
  });

  // -- (G10) plan-mode controls + usage region mobile behaviour -------------
  // The plan-mode selects reuse the modal form styles (full-width controls
  // inside .modal-card), and the usage tables scroll horizontally instead of
  // widening the history pane past the viewport.

  check("G10 DOM: new-task and issue-launch plan-mode selects exist", () => {
    for (const id of [
      "nt-decomposition",
      "nt-granularity",
      "issue-launch-decomposition",
      "issue-launch-granularity",
    ]) {
      const node = document.getElementById(id);
      assert.ok(node, `missing plan-mode select #${id}`);
    }
  });

  check("G10 CSS: usage tables contain horizontal overflow, not layout growth", () => {
    const sel = ".usage-table {";
    const idx = CSS.indexOf(sel);
    assert.notEqual(idx, -1, "missing .usage-table rule");
    const body = CSS.slice(idx, CSS.indexOf("}", idx));
    assert.ok(body.includes("overflow-x: auto"),
      ".usage-table must scroll horizontally on narrow screens");
    assert.ok(body.includes("white-space: nowrap"),
      ".usage-table must keep one line per row so the scroll stays contained");
  });

  check("G10 CSS: history meta + usage region stay inside the detail pane", () => {
    for (const sel of [".history-meta {", ".history-usage-region {"]) {
      const idx = CSS.indexOf(sel);
      assert.notEqual(idx, -1, `missing ${sel} rule`);
    }
    // The meta labels/values must wrap (not widen) on a phone-width pane.
    const metaIdx = CSS.indexOf(".history-meta-block {");
    const metaBody = CSS.slice(metaIdx, CSS.indexOf("}", metaIdx));
    assert.ok(metaBody.includes("flex-wrap: wrap"),
      ".history-meta-block must wrap on narrow screens");
  });
}
